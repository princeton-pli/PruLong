import logging
import os
import sys
import torch
import datasets
import transformers
import functools

from transformers import (
    AutoConfig,
    AutoTokenizer,
    HfArgumentParser,
    set_seed,
)

from training.modeling_flash_llama import PawLlamaForCausalLM, PawLlamaConfig
from training.lh_trainer import Trainer, TrainingArguments
from training.dataset import build_dataset, DataCollator, DataArguments
from training.dataset import logger as dataset_logger


from torch.distributed.fsdp.wrap import lambda_auto_wrap_policy

from transformers.trainer_utils import get_last_checkpoint
import json
from dataclasses import dataclass, field
from typing import Optional, List
from csv import reader


logger = logging.getLogger(__name__)

@dataclass
class ScriptArguments:
    model_name_or_path: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "The model checkpoint for weights initialization.Don't set if you want to train a model from scratch."
            )
        },
    )
    config_overrides: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "Override some existing default config settings when a model is trained from scratch. Example: "
                "n_embd=10,resid_pdrop=0.2,scale_attn_weights=false,summary_type=cls_index"
            )
        },
    )
    config_overrides_json: Optional[str] = field(
        default=None,
        metadata={
            "help": (
                "Override some existing default config settings when a model is trained from scratch. Example: "
                "'{\"resid_pdrop\": 0.2}'"
            )
        },
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
    )
    use_fast_tokenizer: bool = field(
        default=True,
        metadata={"help": "Whether to use one of the fast tokenizer (backed by the tokenizers library) or not."},
    )
    model_revision: str = field(
        default="main",
        metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": (
                "Will use the token generated when running `huggingface-cli login` (necessary to use this script "
                "with private models)."
            )
        },
    )
    should_log_loss: bool = field(
        default=False,
        metadata={"help": "Whether to log loss components during training"}
    )
    token_scaled_loss: bool = field(
        default=False, 
        metadata={
            "help": "Whether to re-scale the loss by the number of valid training tokens instead of averaging loss across sequences and devices. This should be turned on for instruction tuning, especially when using synthetic data, as the valid training tokens vary across devices."
        },
    )

    tokenized_mds_train: List[str] = field(default_factory=list, metadata={"help": "Paths to tokenized training datasets in MDS format"})
    tokenized_mds_validation: List[str] = field(default_factory=list, metadata={"help": "Paths to tokenized validation datasets in MDS format"})
    tokenized_mds_test: List[str] = field(default_factory=list, metadata={"help": "Paths to tokenized test datasets in MDS format"})



def load_masks_from_tsv_file(
    model,
    tsv_file,
    sparsity=None,
    threshold=None,
):
    f = reader(open(tsv_file, 'r'), delimiter='\t')
    masks = [[float(x) for x in row] for row in f]
    if threshold is not None:
        masks = [[float(x > threshold) for x in row] for row in masks]
    # At this point, masks are in [0,1] -- relinearize to [-10, 10]
    masks = [[(2 * x - 1) * 10 for x in row] for row in masks]
    
    model.load_masks(masks)

    if sparsity is not None:
        model.round_masks_for_sparsity(sparsity)

def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of script_args, for a cleaner separation of concerns.
    parser = HfArgumentParser((ScriptArguments, TrainingArguments, DataArguments))
    script_args, training_args, data_args = parser.parse_args_into_dataclasses()
    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    dataset_logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    logger.info(f"Training/evaluation parameters {training_args}")
    logger.info(f"Additional arguments {script_args}")
    logger.info(f"Data arguments {data_args}")
    # Detecting last checkpoint.
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir):
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is not None and training_args.resume_from_checkpoint is None:
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )

    # Set seed before initializing model.
    set_seed(training_args.seed)
    tokenizer = AutoTokenizer.from_pretrained(
        script_args.tokenizer_name or script_args.model_name_or_path,
        cache_dir=script_args.cache_dir,
        use_fast=script_args.use_fast_tokenizer,
        revision=script_args.model_revision,
        use_auth_token=True if script_args.use_auth_token else None,
    )
    config = PawLlamaConfig.from_pretrained(
        script_args.config_name or script_args.model_name_or_path,
        cache_dir=script_args.cache_dir,
        revision=script_args.model_revision,
        use_auth_token=True if script_args.use_auth_token else None,
        toggle_type=training_args.toggle_type,
        local_window_size=training_args.context_window_if_toggled,
        sink_size=training_args.sink_size,
        disable_linear_regularization_term=training_args.disable_linear_regularization_term,
    )
    if script_args.config_overrides:
        logger.info(f"Overriding config: {script_args.config_overrides}")
        config.update_from_string(script_args.config_overrides)
        logger.info(f"New config: {config}")

    if script_args.config_overrides_json:
        logger.info(f"Overriding config: {script_args.config_overrides_json}")
        config.update(json.loads(script_args.config_overrides_json))
        logger.info(f"New config: {config}")

    config.pad_token_id = 0

    if script_args.model_name_or_path:
        model = PawLlamaForCausalLM.from_pretrained(
            script_args.model_name_or_path,
            from_tf=bool(".ckpt" in script_args.model_name_or_path),
            config=config,
            cache_dir=script_args.cache_dir,
            revision=script_args.model_revision,
            use_auth_token=True if script_args.use_auth_token else None,
        )
    else:
        logger.warning(f"Initializing new PawLlamaForCausalLM from scratch")
        model = PawLlamaForCausalLM(
            config,
        )
        
    # Last time with the Edge Pruning classes, we needed to reset the log alphas if loading from an HF checkpoint
    model.reset_masks()
        
    if training_args.stripe_init_width_1 is not None:
        # We should initialize with a striped pattern
        assert training_args.stripe_init_width_2 is not None, "If stripe_init_width_1 is set, stripe_init_width_2 must be set as well"
        logger.info(f"Initializing with a striped pattern: ({training_args.stripe_init_width_1}, {training_args.stripe_init_width_2})")
        if not training_args.freeze_mask_parameters:
            logger.warning("Stripe initialization without freezing mask parameters is not recommended")
        
        model.reset_masks_with_stripe_pattern(
            training_args.stripe_init_width_1, 
            training_args.stripe_init_width_2, 
            start_with_keep=training_args.stripe_init_start_with_keep
        )
    elif training_args.load_masks_from is not None:
        logger.info(f"Loading masks from {training_args.load_masks_from}")
        load_masks_from_tsv_file(
            model,
            training_args.load_masks_from,
            sparsity=training_args.load_masks_sparsity,
        )

    if script_args.tokenizer_name is not None and script_args.model_name_or_path != script_args.tokenizer_name:
        model.resize_token_embeddings(len(tokenizer))

    logger.info(f"Model: {model}")

    # Idk causes weird issues without this when doing multiple runs from different codebases
    import streaming
    streaming.base.util.clean_stale_shared_memory()
    
    if script_args.token_scaled_loss:
        model.token_scaled_loss = True
        training_args.token_scaled_loss = True

    # load_datasets
    if training_args.do_train:
        train_dataset = build_dataset(script_args.tokenized_mds_train, training_args, data_args, is_training=True)

    if training_args.do_eval:
        eval_dataset = {
            x.split("/")[-1]: build_dataset([x], training_args, data_args, is_training=False)
            for x in script_args.tokenized_mds_validation
        }

    if training_args.do_predict:
        test_dataset = {
            x.split("/")[-1]: build_dataset([x], training_args, data_args, is_training=False)
            for x in script_args.tokenized_mds_test
        }

    data_collator = DataCollator(tokenizer, data_args)
    assert training_args.max_steps is not None, "max_steps must be set!"
    


    # Initialize our Trainer
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset if training_args.do_train else None,
        eval_dataset=eval_dataset if training_args.do_eval else None,
        tokenizer=tokenizer,
        data_collator=data_collator,
        log_loss=script_args.should_log_loss,
    )

    if trainer.is_fsdp_enabled:
        # Identify which modules have "_fsdp_wrap" attribute set to True and wrap these
        def fsdp_policy_fn(module):
            return getattr(module, "_fsdp_wrap", False)

        auto_wrap_policy = functools.partial(lambda_auto_wrap_policy,
                                             lambda_fn=fsdp_policy_fn)
        trainer.accelerator.state.fsdp_plugin.auto_wrap_policy = auto_wrap_policy

    # Training
    if training_args.do_train:
        checkpoint = None
        if training_args.resume_from_checkpoint is not None:
            checkpoint = training_args.resume_from_checkpoint
        elif last_checkpoint is not None:
            checkpoint = last_checkpoint
        train_result = trainer.train(resume_from_checkpoint=checkpoint)
        trainer.save_model()

        metrics = train_result.metrics
        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()

    # Evaluation
    if training_args.do_eval:
        logger.info("*** Evaluate ***")
        metrics = trainer.evaluate(eval_dataset)
        trainer.log_metrics("eval", metrics)
        trainer.save_metrics("eval", metrics)

    # Predict
    if training_args.do_predict:
        logger.info("*** Predict ***")
        predictions = trainer.predict(test_dataset=test_dataset)
        predictions = predictions.predictions
        predictions = tokenizer.batch_decode(predictions, skip_special_tokens=True)
        
        # Save predictions to output directory
        output_file = os.path.join(training_args.output_dir, 'predictions.json')
        with open(output_file, 'w') as f:
            json.dump(predictions, f, indent=2)


if __name__ == "__main__":
    main()
