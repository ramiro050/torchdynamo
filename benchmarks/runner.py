#!/usr/bin/env python

"""
A wrapper over the benchmark infrastructure to generate commonly used commands,
parse results and generate csv/graphs.

The script works on manually written TABLE (see below). We can add more commands
in the future.

One example usage is
-> python benchmarks/runner.py --suites=torchbench --inference
This command will generate the commands for the default compilers (see DEFAULTS
below) for inference, run them and visualize the logs.

If you want to just print the commands, you could use the following command
-> python benchmarks/runner.py --print_run_commands --suites=torchbench --inference

Similarly, if you want to just visualize the already finished logs
-> python benchmarks/runner.py --visualize_logs --suites=torchbench --inference

If you want to test float16
-> python benchmarks/runner.py --suites=torchbench --inference --dtypes=float16

"""

import argparse
import io
import itertools
import os
from os.path import exists

import matplotlib.pyplot as plt
import pandas as pd
import torch
from matplotlib import rcParams
from scipy.stats import gmean
from scipy.stats import tmean
from tabulate import tabulate

import torchdynamo

rcParams.update({"figure.autolayout": True})
plt.rc("axes", axisbelow=True)

DEFAULT_OUTPUT_DIR = "benchmark_logs"


TABLE = {
    "training": {
        "ts_nnc": "--training --speedup-ts --use-eval-mode --isolate",
        "ts_nvfuser": "--training --nvfuser --speedup-dynamo-ts --use-eval-mode --isolate",
        "aot_eager": "--training --accuracy-aot-nop --generate-aot-autograd-stats --use-eval-mode --isolate",
        "aot_nnc": "--training --accuracy-aot-ts-mincut --use-eval-mode --isolate",
        "aot_nvfuser": "--training --nvfuser --accuracy-aot-ts-mincut --use-eval-mode --isolate",
        "inductor_cudagraphs": "--training --inductor --use-eval-mode --isolate",
    },
    "inference": {
        "ts_nnc": "--isolate --speedup-ts",
        "ts_nvfuser": "--isolate -n100 --speedup-ts --nvfuser",
        "trt": "--isolate -n100 --speedup-trt",
        "eager_cudagraphs": "--inductor-settings --float32 -n50 --backend=cudagraphs",
        "nnc_cudagraphs": "--inductor-settings --float32 -n50 --backend=cudagraphs_ts --nvfuser",
        "ts_nvfuser_cudagraphs": "--inductor-settings --float32 -n50 --backend=cudagraphs_ts",
        "inductor_cudagraphs": "--inductor-settings --float32 -n50 --inductor",
    },
    "coverage": {"dynamo_eager": "--isolate --coverage"},
}

INFERENCE_COMPILERS = tuple(TABLE["inference"].keys())
TRAINING_COMPILERS = tuple(TABLE["training"].keys())

DEFAULTS = {
    "training": ["ts_nvfuser", "aot_nvfuser", "inductor_cudagraphs"],
    "inference": ["ts_nvfuser_cudagraphs", "inductor_cudagraphs"],
    "coverage": ["dynamo_eager"],
    "dtypes": [
        "float32",
    ],
    "suites": ["torchbench", "huggingface", "timm_models"],
    "devices": [
        "cuda",
    ],
}


def percentage(part, whole):
    return round(100 * float(part) / float(whole), 2)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--devices", action="append", help="cpu or cuda")
    parser.add_argument("--dtypes", action="append", help="float16/float32/amp")
    parser.add_argument("--suites", action="append", help="huggingface/torchbench/timm")
    parser.add_argument(
        "--compilers",
        action="append",
        help=f"For --inference, options are {INFERENCE_COMPILERS}. For --training, options are {TRAINING_COMPILERS}",
    )
    parser.add_argument(
        "--quick", action="store_true", help="Just runs one model. Helps in debugging"
    )
    parser.add_argument(
        "--output-dir", help="Choose the output directory to save the logs"
    )

    # Choose either generation of commands, pretty parsing or e2e runs
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--print_run_commands",
        action="store_true",
        help="Generate commands and saves them to run.sh",
    )
    group.add_argument(
        "--visualize_logs",
        action="store_true",
        help="Pretty print the log files and draw graphs",
    )
    group.add_argument(
        "--run",
        action="store_true",
        default=True,
        help="Generate commands, run and parses the files",
    )

    # Choose either inference or training
    group_mode = parser.add_mutually_exclusive_group(required=True)
    group_mode.add_argument(
        "--inference", action="store_true", help="Only run inference related tasks"
    )
    group_mode.add_argument(
        "--training", action="store_true", help="Only run training related tasks"
    )
    group_mode.add_argument(
        "--coverage", action="store_true", help="Runs coverage experiment"
    )

    args = parser.parse_args()
    return args


def get_mode(args):
    if args.inference:
        return "inference"
    elif args.training:
        return "training"
    else:
        assert args.coverage
        return "coverage"


def generate_commands(args, dtypes, suites, devices, compilers, output_dir):
    mode = get_mode(args)
    with open("run.sh", "w") as runfile:
        lines = []

        lines.append("# Setup the output directory")
        lines.append(f"rm -rf {output_dir}")
        lines.append(f"mkdir {output_dir}")
        lines.append("")

        for iter in itertools.product(suites, devices, dtypes):
            suite, device, dtype = iter
            lines.append(
                f"# Commands for {suite} for device={device}, dtype={dtype} for {mode}"
            )
            info = TABLE[mode]
            for compiler in compilers:
                base_cmd = info[compiler]
                output_filename = (
                    f"{output_dir}/{compiler}_{suite}_{dtype}_{mode}_{device}.csv"
                )
                cmd = f"python benchmarks/{suite}.py --{dtype} -d{device} --no-skip --output={output_filename}"
                cmd = f"{cmd} {base_cmd}"
                if args.quick:
                    if suite == "torchbench":
                        cmd = f"{cmd} --only=resnet18"
                    elif suite == "huggingface":
                        cmd = f"{cmd} --only=BertForPreTraining_P1_bert"
                    else:
                        raise NotImplementedError(
                            f"Quick not implemented for {suite}.py"
                        )
                lines.append(cmd)
            lines.append("")
        runfile.writelines([line + "\n" for line in lines])


def pp_dataframe(df, title, output_dir, out_io=None, draw_graph=True):
    # Pretty print
    if out_io is not None:
        out_io.write("\n")
        out_io.write("~~~\n")
        out_io.write(f"Results for {title}\n")
        out_io.write(tabulate(df, headers="keys", tablefmt="pretty", showindex="never"))
        out_io.write("\n")
        out_io.write("~~~\n")

    # Save to csv, can be copy pasted in google sheets
    df.to_csv(f"{output_dir}/{title}.csv", index=False)

    # Graph
    if draw_graph:
        labels = df.columns.values.tolist()
        labels = labels[3:]
        df.plot(
            x="name",
            y=labels,
            kind="bar",
            title=title,
            ylabel="Speedup over eager",
            xlabel="",
            grid=True,
            figsize=(max(len(df.index) / 4, 5), 10),
            edgecolor="black",
        )
        plt.tight_layout()
        plt.savefig(f"{output_dir}/{title}.png")


def build_summary():
    import git

    out_io = io.StringIO()

    def print_commit_hash(path, name):
        if exists(path):
            repo = git.Repo(path, search_parent_directories=True)
            sha = repo.head.object.hexsha
            out_io.write(f"{name} commit: {sha}\n")
        else:
            out_io.write(f"{name} Absent\n")

    def env_var(name):
        out_io.write(f"{name} = {os.environ[name]}\n")

    out_io.write("## Commit hashes ##\n")
    print_commit_hash(".", "torchdynamo")
    print_commit_hash("../pytorch", "pytorch")
    print_commit_hash("../functorch", "functorch")
    print_commit_hash("../torchbenchmark", "torchbench")

    out_io.write("\n")
    out_io.write("## TorchDynamo config flags ##\n")
    for key in dir(torchdynamo.config):
        val = getattr(torchdynamo.config, key)
        if not key.startswith("__") and isinstance(val, bool):
            out_io.write(f"torchdynamo.config.{key} = {val}\n")

    out_io.write("\n")
    out_io.write("## Torch version ##\n")
    out_io.write(f"torch: {torch.__version__}\n")

    out_io.write("\n")
    out_io.write("## Environment variables ##\n")
    env_var("TORCH_CUDA_ARCH_LIST")
    env_var("CUDA_HOME")
    env_var("USE_LLVM")

    out_io.write("\n")
    out_io.write("## GPU details ##\n")
    out_io.write(f"CUDNN VERSION: {torch.backends.cudnn.version()}\n")
    out_io.write(f"Number CUDA Devices: {torch.cuda.device_count()}\n")
    out_io.write(f"Device Name: {torch.cuda.get_device_name(0)}\n")
    out_io.write(
        f"Device Memory [GB]: {torch.cuda.get_device_properties(0).total_memory/1e9}\n"
    )
    with open(f"{output_dir}/gh_build_summary.txt", "w") as gh_fh:
        gh_fh.write(out_io.getvalue())


def read_csv(output_filename):
    has_header = False
    n_cols = 3
    with open(output_filename, "r") as f:
        line = f.readline()
        if "dev" in line:
            has_header = True
            n_cols = len(line.rstrip().split())

    if has_header:
        return pd.read_csv(output_filename)
    else:
        assert n_cols == 3
        return pd.read_csv(
            output_filename, names=["dev", "name", "batch_size", "speedup"], header=None
        )


def parse_coverage_logs(args, dtypes, suites, devices, compilers, output_dir):
    def sorted_pretty_print(df, key, out_op):
        title = f"{suite}_{dtype}_{mode}_{device}"
        sorted_df = df.sort_values(by=key, ascending=False)
        col = sorted_df.pop(key)
        sorted_df.insert(3, key, col)
        pp_dataframe(
            sorted_df,
            f"sorted_{title}_{key}",
            output_dir,
            out_io=out_io,
            draw_graph=False,
        )
        out_io.write("\n\n")

    def analyze_graph_breaks(df, out_io):
        # Analysis number of graphs
        num_models = len(df.index)
        no_graph_breaks = (df.graphs == 1).sum()
        perc = percentage(no_graph_breaks, num_models)

        out_io.write("**Graph Breaks**\n")
        out_io.write(f"Number of models = {num_models}\n")
        out_io.write(f"Number of models with no graph breaks = {no_graph_breaks}\n")
        out_io.write(f"Percentage of models with no graph breaks = {perc}%")

        # Sort the dataframe and pretty print
        df_graphs = df[df.graphs != 1]
        sorted_pretty_print(df_graphs, "graphs", out_io)

    def analyze_start_latency(df, out_io):
        # Analysis start_latency
        num_models = len(df.index)
        low_latency_models = (df.start_latency < 5.0).sum()
        perc = percentage(low_latency_models, num_models)

        out_io.write("**Start Latency - Rough approximation of compile latency**\n")
        out_io.write(f"Number of models = {num_models}\n")
        out_io.write(
            f"Number of models with low start latency = {low_latency_models}\n"
        )
        out_io.write(f"Percentage of models with low start latency = {perc}%")

        # Sort the dataframe and pretty print
        df_high_latency = df[df.start_latency > 5]
        sorted_pretty_print(df_high_latency, "start_latency", out_io)

    mode = "coverage"
    out_io = io.StringIO()
    out_io.write("\n")
    out_io.write("## Coverage results ##\n")
    frames = []
    for iter in itertools.product(suites, devices, dtypes):
        suite, device, dtype = iter
        # Collect results from all the files
        for compiler in compilers:
            output_filename = (
                f"{output_dir}/{compiler}_{suite}_{dtype}_{mode}_{device}.csv"
            )

            df = read_csv(output_filename)
            df.insert(1, "suite", suite)
            frames.append(df)

    # Merge the results
    if len(frames) == 1:
        df = frames[0]
    else:
        df = pd.concat(frames)

    analyze_graph_breaks(df, out_io)
    analyze_start_latency(df, out_io)

    print(out_io.getvalue())
    with open(f"{output_dir}/gh_coverage.txt", "w") as gh_fh:
        gh_fh.write(out_io.getvalue())


def parse_logs(args, dtypes, suites, devices, compilers, output_dir):
    mode = get_mode(args)
    build_summary()

    if args.coverage:
        parse_coverage_logs(args, dtypes, suites, devices, compilers, output_dir)
        return

    out_io = io.StringIO()
    out_io.write("\n")
    out_io.write("## Performance results ##\n")
    for iter in itertools.product(suites, devices, dtypes):
        suite, device, dtype = iter
        frames = []
        # Collect results from all the files
        for compiler in compilers:
            output_filename = (
                f"{output_dir}/{compiler}_{suite}_{dtype}_{mode}_{device}.csv"
            )

            df = read_csv(output_filename)
            df.rename(
                columns={"speedup": compiler, "ts": compiler, "ofi": f"ofi_{compiler}"},
                inplace=True,
            )
            frames.append(df)

        # Merge the results
        if len(compilers) == 1:
            df = frames[0]
        else:
            # Clean up batch sizes when its 0
            batch_sizes = frames[0]["batch_size"].to_list()
            for frame in frames[1:]:
                frame_batch_sizes = frame["batch_size"].to_list()
                for idx, (batch_a, batch_b) in enumerate(
                    zip(batch_sizes, frame_batch_sizes)
                ):
                    assert batch_a == batch_b or batch_a == 0 or batch_b == 0, print(
                        f"a={batch_a}, b={batch_b}"
                    )
                    batch_sizes[idx] = max(batch_a, batch_b)
            for frame in frames:
                frame["batch_size"] = batch_sizes

            # Merge data frames
            df = pd.merge(frames[0], frames[1], on=["dev", "name", "batch_size"])
            for idx in range(2, len(frames)):
                df = pd.merge(df, frames[idx], on=["dev", "name", "batch_size"])

        df["batch_size"] = df["batch_size"].astype(int)
        # Pretty print and also write to a bargraph
        title = f"{suite}_{dtype}_{mode}_{device}"
        pp_dataframe(df, title, output_dir)

        # Add geomean and mean
        for compiler in compilers:
            speedups = df[compiler].clip(1)
            geo_mean = round(gmean(speedups), 3)
            mean = round(tmean(speedups), 3)
            out_io.write(
                "{:<30}: gmean_speedup = {:.2f}x, mean_speedup = {:.2f}x\n".format(
                    compiler, geo_mean, mean
                )
            )

        # Sort the dataframe and pretty print
        sorted_df = df.sort_values(by=list(reversed(compilers)), ascending=False)
        pp_dataframe(sorted_df, f"sorted_{title}", output_dir, out_io=out_io)
    print(out_io.getvalue())
    with open(f"{output_dir}/gh_performance.txt", "w") as gh_fh:
        gh_fh.write(out_io.getvalue())


if __name__ == "__main__":
    args = parse_args()

    def extract(key):
        return DEFAULTS[key] if getattr(args, key, None) is None else getattr(args, key)

    dtypes = extract("dtypes")
    suites = extract("suites")
    devices = extract("devices")

    if args.inference:
        compilers = DEFAULTS["inference"] if args.compilers is None else args.compilers
    elif args.training:  # args.training
        compilers = DEFAULTS["training"] if args.compilers is None else args.compilers
    else:
        assert args.coverage
        assert args.compilers is None
        compilers = DEFAULTS["coverage"]

    output_dir = args.output_dir if args.output_dir is not None else DEFAULT_OUTPUT_DIR

    if args.print_run_commands:
        generate_commands(args, dtypes, suites, devices, compilers, output_dir)
    elif args.visualize_logs:
        parse_logs(args, dtypes, suites, devices, compilers, output_dir)
    elif args.run:
        generate_commands(args, dtypes, suites, devices, compilers, output_dir)
        # TODO - Do we need to worry about segfaults
        try:
            os.system("bash run.sh")
        except Exception as e:
            print(
                "Running commands failed. Please run manually (bash run.sh) and inspect the errors."
            )
            raise e
        parse_logs(args, dtypes, suites, devices, compilers, output_dir)
