import subprocess, os, signal
import pyprob
from pyprob import RemoteModel
import numpy as np
from pathlib import Path
import torch
from types import SimpleNamespace
from sacred import Experiment
import json
import zipfile
import tempfile

FRED_HOME = os.environ['FRED_HOME']
model_executable = f'{FRED_HOME}/bin/FRED'
HOME = os.environ['HOME']
USER = os.environ['USER']
default_params = None
fips_dict = {'jefferson': 42065,
             'allegheny': 42003}

# Use sacred for command line interface + hyperparams
ex = Experiment()

@ex.config
def my_config():
    # paths
    params_base = f'{FRED_HOME}/params'
    out_level_1 = f'results'
    out_level_2 = 'experiment_name'
    out_level_3 = 'simulation_number'
    tmp_directory = None # If not None, writes the outputs on level_1=tmp_directory and stores the tar.gz compressed output in the out_dir
    debug = False
    dump_simulator_log = True

    # Simulator parameters
    days = None
    county = 'jefferson'
    assert county in fips_dict
    _fips = fips_dict[county]

    # Inference-related parameters
    num_traces = 10
    kill_on_zero_likelihood = False
    constraint_threshold = 0.1


def read_param_file(path):
    with open(path, 'r') as f:
        param_lines = f.readlines()
    param_lines = map(lambda x: x.strip(), param_lines)
    param_lines = list(filter(lambda x: not(x.startswith('#') or x == ''), param_lines))
    params_dict = {}
    for param_line in param_lines:
        parts = param_line.split('=')
        name = parts[0].strip()
        value = parts[1].strip()
        params_dict[name] = value
    return params_dict

def get_default_params():
    global default_params
    if default_params is not None:
        return default_params.copy()
    defaults = 'defaults'
    if not os.path.exists(defaults):
        defaults = os.path.join(FRED_HOME, 'input_files', 'defaults')
    if not os.path.exists(defaults):
        raise Exception('could not find defaults file')
    params = read_param_file(defaults)
    default_params = params.copy()
    return params

def dump_parameter_file(path='', args=None, sampled_parameters={}):
    params = get_default_params()
    params.update(read_param_file(args.params))
    params.update(sampled_parameters)
    with open(path, 'w') as f:
        for param, value in params.items():
            f.write(f'{param} = {value}\n')


def init(config, seed):
    # This gives dot access to all paths, hyperparameters, etc
    pyprob.set_random_seed(seed)

    args = SimpleNamespace(**config)
    if args.tmp_directory is None:
        out_dir = Path(args.out_level_1) / args.out_level_2 / args.out_level_3
        args.compressed_file_path = None
    else:
        args.tmp_directory = tempfile.mktemp(dir=args.tmp_directory)
        out_dir = Path(args.tmp_directory) / args.out_level_2 / args.out_level_3
        compressed_file_path = Path(args.out_level_1) / args.out_level_2 / f'{args.out_level_3}.zip'
        if not args.debug:
            assert not compressed_file_path.exists()
        compressed_file_path.parent.mkdir(parents=True, exist_ok=True)
        args.compressed_file_path = str(compressed_file_path)

    out_dir.mkdir(parents=True, exist_ok=args.debug)
    args.out_dir = str(out_dir)
    base_params = read_param_file(args.params_base)

    # Set the county fips and days
    base_params['fips'] = args._fips

    if args.days is not None:
        base_params['days'] = args.days
    else:
        if 'days' in base_params:
            args.days = int(base_params['days'])
        else:
            args.days = int(get_default_params()['days'])
    
    args.params = os.path.join(args.out_dir, 'params_generated')
    with open(args.params, 'w') as f:
        for param, value in base_params.items():
            f.write(f'{param} = {value}\n')

    with open(os.path.join(args.out_dir, 'config.json'), 'w') as fp:
        json.dump(args.__dict__, fp,
                  indent=4, separators=(',', ': '))
    return args


def zipdir(path, dir_path):
    pwd = os.getcwd()
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_BZIP2) as zip_f:
        os.chdir(dir_path)
        for root, dirs, files in os.walk('.'):
            for cur_file in files:
                zip_f.write(os.path.join(root, cur_file))
    os.chdir(pwd)


def run(args):
    def model_dispatcher(trace_idx, server_address):
        arguments = f'{args.params} {trace_idx} {args.out_dir}'
        if args.dump_simulator_log:
            return subprocess.Popen(f'{model_executable} {server_address} {arguments} 2>&1 > {args.out_dir}/LOG{trace_idx} &', shell=True, preexec_fn=os.setsid)
        else:
            return subprocess.Popen(f'{model_executable} {server_address} {arguments} 2>&1 &', shell=True, preexec_fn=os.setsid)

    try:
        model = RemoteModel(random_server_address=True,
                            model_dispatcher=model_dispatcher,
                            restart_per_trace=True,
                            kill_on_zero_likelihood=args.kill_on_zero_likelihood)
        traces = model.posterior(num_traces=args.num_traces, inference_engine=pyprob.InferenceEngine.IMPORTANCE_SAMPLING,
                                 observe={f'obs_{i}': args.constraint_threshold for i in range(args.days)})
        trace_weights = {}
        for idx, trace in enumerate(traces):
            # Convert the latent variables that are converted to integer on C++ code.
            trace.named_variables['shelter_in_place_duration_mean'].value = trace.named_variables['shelter_in_place_duration_mean'].value.int()
            
            dump_parameter_file(sampled_parameters={name : variable.value.item() for name, variable in trace.named_variables.items() if not variable.observed},
                                path=os.path.join(args.out_dir, f'params{idx}'), args=args)
            weight = np.exp(trace.log_importance_weight)
            print(f'likelihood {idx}: {weight}')
            assert weight < 0.2 or weight > 0.8
            trace_weights[idx] = int(weight > 0.5)

        print(f'Average success rate: {np.mean(list(trace_weights.keys()))}')

        # Save the trace weights
        with open(os.path.join(args.out_dir, 'weights.json'), 'w') as fp:
            json.dump(trace_weights, fp,
                      indent=4, separators=(',', ': '))
        # Save the traces to file
        traces.copy(file_name=os.path.join(args.out_dir, f'traces'))
    except Exception as e:
        if args.compressed_file_path is not None:
            # Compress the outputs
            print('Failed... Compressing the output')
            zipdir(f'{args.compressed_file_path}_failed', args.out_dir)
        raise e
    finally:
        if model._model_process is not None:
            print('Done, killing model process: {}'.format(model._model_process.pid))
            os.killpg(os.getpgid(model._model_process.pid), signal.SIGTERM)

    
    if args.compressed_file_path is not None:
        # Compress the outputs
        print('Compressing the output')
        zipdir(args.compressed_file_path, args.out_dir)

@ex.automain
def command_line_entry(_run,_config, _seed):
    args = init(_config, _seed)
    run(args)
