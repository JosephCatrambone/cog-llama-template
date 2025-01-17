import argparse
import os
import shutil
from subprocess import call
import logging
from typing import Optional
from zipfile import ZipFile

import torch
from cog import BaseModel, Input, Path
from tensorizer import TensorSerializer
from transformers import LlamaForCausalLM

from config import DEFAULT_MODEL_NAME, pull_gcp_file

MODEL_OUT = "/src/tuned_weights.tensors"
CHECKPOINT_DIR = "checkpoints"
SAVE_STRATEGY = "epoch"
DIST_OUT_DIR = "tmp/model"


class TrainingOutput(BaseModel):
    weights: Path


def train(
    train_data: Path = Input(
        description="path to data file to use for fine-tuning your model"
    ),
    eval_data: Path = Input(
        description="path to optional evaluation data file to use for model eval",
        default=None,
    ),
    weights: Path = Input(
        description="location of weights that are going to be fine-tuned", default=None
    ),
    train_batch_size: int = Input(description="batch size per GPU", default=1, ge=1),
    gradient_accumulation_steps: int = Input(
        description="number of training steps to update gradient for before performing a backward pass",
        default=8,
    ),
    learning_rate: float = Input(
        description="learning rate, for learning!", default=2e-5, ge=0
    ),
    warmup_ratio: float = Input(
        description="pct of steps for a linear learning rate warmup",
        ge=0,
        le=0.5,
        default=0.03,
    ),
    num_train_epochs: int = Input(
        description="number of training epochs", ge=1, default=1
    ),
    max_steps: int = Input(
        description="number of steps to run training for, supersedes num_train_epochs",
        default=-1,
    ),
    logging_steps: int = Input(
        description="number of steps between logging epoch & loss", default=1
    ),
    lora_rank: int = Input(
        description="Rank of the lora matrices", default=8, ge=1),
    lora_alpha: int = Input(description="Alpha parameter for scaling lora weights; weights are scaled by alpha/rank", default=16, ge=1),
    lora_dropout: float = Input(description="Dropout for lora training", default=0.1, ge=0.0, le=1.0),
    lora_target_modules: str = Input(description="Comma-separated list of lora modules to target, i.e. 'q_proj,v_proj'. Leave blank for default.", default="q_proj,v_proj")
) -> TrainingOutput:
    input_weights = weights if weights is not None else DEFAULT_MODEL_NAME

    if 'http' in input_weights or 'gs' in input_weights:
        # doing this once instead of 4x
        local_weights = '/src/llama.tensors'
        pull_gcp_file(input_weights, local_weights)
        input_weights = local_weights

    root_path = os.getcwd()
    deepspeed_config = os.path.join(root_path, "ds_config/ds_z3_bf16_config.json")

    output_dir = DIST_OUT_DIR
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir)

    num_gpus = torch.cuda.device_count()
    num_gpus_flag = f"--num_gpus={num_gpus}"

    print(f"Local Output Dir: {output_dir}")
    print(f"Number of GPUs: {num_gpus}")

    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    os.environ["HF_DATASETS_CACHE"] = "/src/.hf-cache"

    def _arg_if_present(var, var_name):
        """Need to wrap any arguments whose default value in train() is `None`"""
        if var:
            return f" --{var_name} {var}"
        return " "

    res = call(
        "deepspeed "
        + num_gpus_flag
        + " --master_port=9292"
        + " --module training.trainer"
        + f" --deepspeed {deepspeed_config}"
        + f" --train_data={str(train_data)}"
        + f" --weights={input_weights}"
        + f" --num_train_epochs={num_train_epochs}"
        + f" --max_steps={max_steps}"
        + _arg_if_present(eval_data, "eval_data")
        + f" --learning_rate {learning_rate}"
        + f" --train_batch_size {train_batch_size}"
        + f" --gradient_accumulation_steps {gradient_accumulation_steps}"
        + f" --logging_steps {logging_steps}"
        + f" --warmup_ratio {warmup_ratio}"
        + f" --lora_rank {lora_rank}"
        + f" --lora_alpha {lora_alpha}"
        + f" --lora_dropout {lora_dropout}"
        + _arg_if_present(lora_target_modules, "lora_target_modules")
        + " --local_output_dir "
        + output_dir,
        shell=True,
    )
    if res != 0:
        raise Exception(f"Training failed! Process returned error code {res}. Check the logs for details.")
    
    out_path = "training_output.zip"

    directory = Path(output_dir)
    with ZipFile(out_path, "w") as zip:
        for file_path in directory.rglob("*"):
            print(file_path)
            zip.write(file_path, arcname=file_path.relative_to(directory))

    return TrainingOutput(weights=Path(out_path))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fine-tune a language model on a text dataset"
    )
    parser.add_argument(
        "--train_data", type=Path, required=True, help="Path to the json dataset"
    )
    parser.add_argument(
        "--eval_data",
        type=Path,
        required=False,
        help="Path to the json dataset",
        default=None,
    )
    parser.add_argument(
        "--weights",
        type=str,
        default=None,
        help="The model class to fine-tune on HF or as a local path (e.g. 'google/flan-t5-xxl'",
    )
    parser.add_argument(
        "--num_train_epochs", type=int, required=True, help="Number of training epochs"
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=2e-5,
        help="Learning rate for the optimizer",
    )
    parser.add_argument(
        "--train_batch_size", type=int, default=4, help="Batch size for training"
    )
    parser.add_argument(
        "--warmup_ratio",
        type=float,
        default=0.03,
        help="Number of warmup steps for the learning rate scheduler",
    )
    parser.add_argument(
        "--max_steps",
        type=int,
        default=0,
        help="Number of training steps to run, overrides num_train_epochs, useful for testing",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=8,
        help="Number of training steps to run, overrides num_train_epochs, useful for testing",
    )
    parser.add_argument("--logging_steps", type=int, default=1)
    some_args = parser.parse_args()
    train(**vars(some_args))
