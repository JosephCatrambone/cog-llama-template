import shutil
import time
from typing import Optional
import zipfile

import torch
from cog import BasePredictor, ConcatenateIterator, Input, Path
from vllm import EngineArgs, LLMEngine, SamplingParams

from config import DEFAULT_MODEL_NAME, load_tokenizer, load_tensorizer, pull_gcp_file
from subclass import YieldingLlama
from peft import PeftModel
import os


class Predictor(BasePredictor):
    def setup(self, weights: Optional[Path] = None):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if weights is not None and weights.name == "weights":
            # bugfix
            weights = None
        
        weights = DEFAULT_MODEL_NAME if weights is None else str(weights)

        if '.zip' in weights:
            self.model = self.load_peft(weights)
        elif "tensors" in weights:
            self.model = load_tensorizer(weights, plaid_mode=True, cls=YieldingLlama)
        else:
            self.model = self.load_huggingface_model(weights=weights)

        self.tokenizer = load_tokenizer()

    def load_peft(self, weights):
        st = time.time()
        if 'tensors' in DEFAULT_MODEL_NAME:
            model = load_tensorizer(DEFAULT_MODEL_NAME, plaid_mode=False, cls=YieldingLlama)
        else:
            model = self.load_huggingface_model(DEFAULT_MODEL_NAME)
        if 'https' in weights: # weights are in the cloud
            local_weights = 'local_weights.zip'
            pull_gcp_file(weights, local_weights)
            weights = local_weights
        out = '/src/peft_dir'
        if os.path.exists(out):
            shutil.rmtree(out)
        with zipfile.ZipFile(weights, 'r') as zip_ref:
            zip_ref.extractall(out)
        model = PeftModel.from_pretrained(model, out)
        print(f"peft model loaded in {time.time() - st}")
        return model.to('cuda')

    def load_huggingface_model(self, weights=None):
        st = time.time()
        print(f"loading weights from {weights} w/o tensorizer")
        model = YieldingLlama.from_pretrained(
            weights, cache_dir="pretrained_weights", torch_dtype=torch.float16
        )
        model.to(self.device)
        print(f"weights loaded in {time.time() - st}")
        return model

    def predict(
        self,
        prompt: str = Input(description=f"Prompt to send to Llama v2."),
        max_length: int = Input(
            description="Maximum number of tokens to generate. A word is generally 2-3 tokens",
            ge=1,
            default=500,
        ),
        temperature: float = Input(
            description="Adjusts randomness of outputs, greater than 1 is random and 0 is deterministic, 0.75 is a good starting value.",
            ge=0.01,
            le=5,
            default=0.5,
        ),
        top_p: float = Input(
            description="When decoding text, samples from the top p percentage of most likely tokens; lower to ignore less likely tokens",
            ge=0.01,
            le=1.0,
            default=1.0,
        ),
        repetition_penalty: float = Input(
            description="Penalty for repeated words in generated text; 1 is no penalty, values greater than 1 discourage repetition, less than 1 encourage it.",
            ge=0.01,
            le=5,
            default=1,
        ),
        debug: bool = Input(
            description="provide debugging output in logs", default=False
        ),
    ) -> ConcatenateIterator[str]:
        prompt = "User: " + prompt + '\nAssistant: '#Uncomment if you want to use for demo with no chat memory.
        input = self.tokenizer(prompt, return_tensors="pt").input_ids.to(self.device)

        with torch.inference_mode() and torch.autocast("cuda"):
            first_token_yielded = False
            prev_ids = []
            previous_token_id = None  # This stores the previous token id so we can look for `\nUser:`

            for output in self.model.generate(
                input_ids=input,
                max_length=max_length,
                do_sample=True,
                temperature=temperature,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
            ):
                cur_id = output.item()

                # Break if previous token id was 13 (newline) and current id is 2659 (user)
                if previous_token_id == 13 and cur_id == 2659:
                    break

                previous_token_id = cur_id  # Store the current token id to check in the next iteration


                # in order to properly handle spaces, we need to do our own tokenizing. Fun!
                # we're building up a buffer of sub-word / punctuation tokens until we hit a space, and then yielding whole words + punctuation.
                cur_token = self.tokenizer.convert_ids_to_tokens(cur_id)

                # skip initial newline, which this almost always yields. hack - newline id = 13.
                if not first_token_yielded and not prev_ids and cur_id == 13:
                    continue

                # underscore means a space, means we yield previous tokens
                if cur_token.startswith("▁"):  # this is not a standard underscore.
                    # first token
                    if not prev_ids:
                        prev_ids = [cur_id]
                        continue

                    # there are tokens to yield
                    else:
                        token = self.tokenizer.decode(prev_ids)
                        prev_ids = [cur_id]

                        if not first_token_yielded:
                            # no leading space for first token
                            token = token.strip()
                            first_token_yielded = True
                        yield token
                else:
                    prev_ids.append(cur_id)
                    continue

            # remove any special tokens such as </s>
            token = self.tokenizer.decode(prev_ids, skip_special_tokens=True).rstrip('\n')
            if not first_token_yielded:
                # no leading space for first token
                token = token.strip()
                first_token_yielded = True
            yield token 

        if debug:
            print(f"cur memory: {torch.cuda.memory_allocated()}")
            print(f"max allocated: {torch.cuda.max_memory_allocated()}")
            print(f"peak memory: {torch.cuda.max_memory_reserved()}")


class EightBitPredictor(Predictor):
    """subclass s.t. we can configure whether a model is loaded in 8bit mode from cog.yaml"""

    def setup(self, weights: Optional[Path] = None):
        if weights is not None and weights.name == "weights":
            # bugfix
            weights = None
        # TODO: fine-tuned 8bit weights.
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = YieldingLlama.from_pretrained(
            DEFAULT_MODEL_NAME, load_in_8bit=True, device_map="auto"
        )
        self.tokenizer = load_tokenizer()


class VLLMPredictor(BasePredictor):
    def setup(self, preload_testing_tokenizer: bool = False):
        if preload_testing_tokenizer:
            # The tokenizer is one that will let engine load, but we have to replace it after the fact.
            # If we can specify the path to "./llama_weights/llama_tokenizer" instead it will save us a load.
            # This is currently blocked by a protobuf issue(?) on protobuf==4.23.4.
            engine_args = EngineArgs(model="./llama_weights/llama-2-7b-chat/", tokenizer="hf-internal-testing/llama-tokenizer")
            self.engine = LLMEngine.from_engine_args(engine_args)
            # Workaround for tokenizer loading:
            self.tokenizer = load_tokenizer()
            self.engine.tokenizer = self.tokenizer
        else:
            engine_args = EngineArgs(model="./llama_weights/llama-2-7b-chat", tokenizer="./llama_weights/tokenizer")
            self.engine = LLMEngine.from_engine_args(engine_args)
            self.engine.tokenizer.add_special_tokens({"eos_token": "</s>", "bos_token": "</s>", "unk_token": "</s>", "pad_token": "[PAD]"})   
            self.tokenizer = self.engine.tokenizer  # Note that this is flipped from above.

    def predict(
        self,
        prompt: str = Input(description=f"Prompt to send to Llama v2."),
        max_length: int = Input(
            description="Maximum number of tokens to generate. A word is generally 2-3 tokens",
            ge=1,
            default=500,
        ),
        temperature: float = Input(
            description="Adjusts randomness of outputs, greater than 1 is random and 0 is deterministic, 0.75 is a good starting value.",
            ge=0.01,
            le=5,
            default=0.5,
        ),
        top_p: float = Input(
            description="When decoding text, samples from the top p percentage of most likely tokens; lower to ignore less likely tokens",
            ge=0.01,
            le=1.0,
            default=1.0,
        ),
        repetition_penalty: float = Input(
            description="Penalty for repeated words in generated text; 1 is no penalty, values greater than 1 discourage repetition, less than 1 encourage it.",
            ge=0.01,
            le=5,
            default=1,
        ),
        debug: bool = Input(
            description="provide debugging output in logs", default=False
        ),
    ) -> ConcatenateIterator[str]:
        request_id = 0
        prompt = "User: " + prompt + '\nAssistant: '#Uncomment if you want to use for demo with no chat memory.
        sampling_params = SamplingParams(
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_length,
            presence_penalty=repetition_penalty-1,  # presence uses 0 to mean 'no penalty'.
        )
        self.engine.add_request(request_id=request_id, prompt=prompt, sampling_params=sampling_params)

        last_output_length = 0
        while True:
            outputs = self.engine.step()

            # Can we guarantee there will be only one output from this?
            if last_output_length == 0 and not outputs:
                # Possible we're still waiting for the initial reply.
                continue
            elif not outputs:
                break  # We're done.
            else:
                # This is assuming a single prompt.  If we decide to do multiple we have to update this and 'for each' them. 
                assert(len(outputs) == 1)
                output = outputs[0]
                assert(output.request_id == request_id)
                completion = output.outputs[0]  # If n > 1 we will have multiple of these.
                new_text = completion.text
                if new_text:
                    # Yield the substring that the user hasn't seen.
                    yield new_text[last_output_length:]
                    last_output_length = len(new_text)
                if output.finished or completion.finish_reason is not None:
                    break

        if debug:
            print(f"cur memory: {torch.cuda.memory_allocated()}")
            print(f"max allocated: {torch.cuda.max_memory_allocated()}")
            print(f"peak memory: {torch.cuda.max_memory_reserved()}")
