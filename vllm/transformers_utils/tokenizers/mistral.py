# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union, cast

import huggingface_hub
import regex as re
from huggingface_hub import HfApi, hf_hub_download

from vllm.logger import init_logger
from vllm.transformers_utils.tokenizer_base import TokenizerBase
from vllm.utils import is_list_of

if TYPE_CHECKING:
    # make sure `mistral_common` is lazy imported,
    # so that users who only use non-mistral models
    # will not be bothered by the dependency.
    from mistral_common.protocol.instruct.request import ChatCompletionRequest
    from mistral_common.tokens.tokenizers.mistral import (
        MistralTokenizer as PublicMistralTokenizer)

    from vllm.entrypoints.chat_utils import ChatCompletionMessageParam

logger = init_logger(__name__)


@dataclass
class Encoding:
    input_ids: Union[list[int], list[list[int]]]


def maybe_serialize_tool_calls(request: "ChatCompletionRequest"):
    # SEE: https://github.com/vllm-project/vllm/pull/9951
    # Credits go to: @gcalmettes
    # NOTE: There is currently a bug in pydantic where attributes
    # declared as iterables are replaced in in the instances by
    # pydantic-core ValidatorIterator instance. In particular, this
    # affects tool_calls defined in ChatCompletionAssistantMessageParam
    # model:
    # see:
    #   - https://github.com/pydantic/pydantic/issues/9467
    # As a result, tool_calls from assistant messages are never
    # deserialized in the request object if the tool_calls iterator is
    # not consumed. This affect messages passed to the MistralTokenizer
    # since no chat template is applied and therefore the tools_calls
    # iterator is not directly consumed.
    # Issue is tracked on Pydantic side, with resolution planned for
    # v2.11 release. In the meantime, the official workaround is to
    # consume the iterator so the tool_calls are correctly deserialized
    # in the OpenAI ChatCompletionAssistantMessageParam object
    # https://github.com/pydantic/pydantic/issues/9467#issuecomment-2442097291 # noqa: E501
    # Official Pydantic Issues:
    #   - https://github.com/pydantic/pydantic/issues/9541
    # TODO: remove when pydantic v2.11 is released
    for i, message in enumerate(request.messages):
        if message.get("role") == 'assistant':
            tool_calls_validator = message.get("tool_calls", ().__iter__())
            validated_tool_calls = []
            while True:
                try:
                    tool_call = next(tool_calls_validator)  # type: ignore
                    validated_tool_calls.append(tool_call)
                except StopIteration:
                    break

            request.messages[i]["tool_calls"] = validated_tool_calls


def truncate_tool_call_ids(request: "ChatCompletionRequest"):
    """Truncates tool call IDs for Mistral's ID requirements."""
    for i, message in enumerate(request.messages):
        if message.get("role") == 'assistant':
            tool_calls = message.get("tool_calls", [])
            for tool_call in tool_calls:
                if len(tool_call["id"]) > 9:
                    logger.warning(
                        "Truncating tool call ID: %s to %s",
                        tool_call["id"],
                        tool_call["id"][-9:],
                    )
                    tool_call["id"] = tool_call["id"][-9:]

            request.messages[i]["tool_calls"] = tool_calls

        elif message.get("role") in {"tool_results", "tool"}:
            if "tool_call_id" in message:
                tool_call_id = message["tool_call_id"]

                if len(tool_call_id) > 9:
                    logger.warning(
                        "Truncating tool_call_id: %s to %s",
                        tool_call_id,
                        tool_call_id[-9:],
                    )
                    tool_call_id = tool_call_id[-9:]
                request.messages[i]["tool_call_id"] = tool_call_id


def validate_request_params(request: "ChatCompletionRequest"):
    if (request.skip_special_tokens is not None
            and not request.skip_special_tokens):
        raise ValueError("skip_special_tokens=False is not supported "
                         "for Mistral tokenizers.")


def list_local_repo_files(repo_id: str, revision: Optional[str]) -> list[str]:
    repo_cache = os.path.join(
        huggingface_hub.constants.HF_HUB_CACHE,
        huggingface_hub.constants.REPO_ID_SEPARATOR.join(
            ["models", *repo_id.split("/")]))

    if revision is None:
        revision_file = os.path.join(repo_cache, "refs", "main")
        if os.path.isfile(revision_file):
            with open(revision_file) as file:
                revision = file.read()

    if revision:
        revision_dir = os.path.join(repo_cache, "snapshots", revision)
        if os.path.isdir(revision_dir):
            return os.listdir(revision_dir)

    return []


def find_tokenizer_file(files: list[str]):
    file_pattern = re.compile(
        r"^tokenizer\.model\.v.*$|^tekken\.json$|^tokenizer\.mm\.model\.v.*$")

    matched_files = [file for file in files if file_pattern.match(file)]
    if len(matched_files) > 1:
        raise OSError(
            f"Found {len(matched_files)} files matching the "
            f"pattern: `{file_pattern.pattern}`. Make sure only one Mistral "
            f"tokenizer is present in {files}.")
    elif len(matched_files) == 0:
        raise OSError(
            f"Found {len(matched_files)} files matching the "
            f"pattern: `{file_pattern.pattern}`. Make sure that a Mistral "
            f"tokenizer is present in {files}.")

    return matched_files[0]


def _aggregate_content(content: list) -> list[dict[str, Any]]:
    aggregated_content: list[dict[str, Any]] = []
    for chunk in content:
        if chunk.get("type"
                     ) == "text" and aggregated_content and aggregated_content[
                         -1].get("type") == "text":
            aggregated_content[-1]["text"] += "\n\n" + chunk.get("text")
        else:
            aggregated_content.append(chunk)
    if len(aggregated_content) == 1 and aggregated_content[0].get(
            "type") == "text":
        content = aggregated_content[0]["text"]
    return content


def make_mistral_chat_completion_request(
        messages: list["ChatCompletionMessageParam"],
        tools: Optional[list[dict[str,
                                  Any]]] = None) -> "ChatCompletionRequest":
    last_message = cast(dict[str, Any], messages[-1])
    if last_message["role"] == "assistant":
        last_message["prefix"] = True

    # mistral-common requires AssistantMessage content to be string [1].
    #
    # [1]: https://github.com/mistralai/mistral-common/blob/f4a06998b75ed78bbf5aaf569590b772ea26c9f6/src/mistral_common/protocol/instruct/messages.py#L80
    for message in messages:
        # Remove reasoning_content as unsupported by Mistral
        _ = message.pop("reasoning_content", None)  # type: ignore

        # Convert list text content to string
        if message.get("role") in ("assistant", "tool"):
            content: Any = message.get("content")
            if isinstance(content, list):
                content = _aggregate_content(content)
            message["content"] = content

    # The Mistral client, in comparison to the OpenAI client, requires the
    # "parameters" dict and the "description" string to be present
    # even if they are empty.
    if tools:
        for function in [
                tool["function"] for tool in tools
                if tool["type"] == "function"
        ]:
            if function.get("parameters") is None:
                function["parameters"] = {}
            if function.get("description") is None:
                function["description"] = ""

    from mistral_common.protocol.instruct.request import ChatCompletionRequest
    return ChatCompletionRequest(messages=messages,
                                 tools=tools)  # type: ignore[type-var]


class MistralTokenizer(TokenizerBase):

    def __init__(self, tokenizer: "PublicMistralTokenizer") -> None:
        self.mistral = tokenizer
        self.instruct = tokenizer.instruct_tokenizer
        _mistral_version_str = self.instruct.tokenizer.version.value
        self.version: int = int(_mistral_version_str.split("v")[-1])

        tokenizer_ = tokenizer.instruct_tokenizer.tokenizer
        from mistral_common.tokens.tokenizers.tekken import (
            SpecialTokenPolicy, Tekkenizer)
        self.is_tekken = isinstance(tokenizer_, Tekkenizer)
        from mistral_common.tokens.tokenizers.sentencepiece import (
            SentencePieceTokenizer)
        self.is_spm = isinstance(tokenizer_, SentencePieceTokenizer)
        if self.is_tekken:
            # Make sure special tokens will not raise
            tokenizer_.special_token_policy = SpecialTokenPolicy.IGNORE
        elif self.is_spm:
            pass
        else:
            raise TypeError(f"Unsupported tokenizer: {type(tokenizer_)}")

        self._vocab = tokenizer_.vocab()
        # Convert to a dict[str, int] to match protocol, but this is a lossy
        # conversion. There may be multiple token ids that decode to the same
        # string due to partial UTF-8 byte sequences being converted to �
        self._vocab_dict = {
            token: idx
            for idx, token in enumerate(self._vocab)
        }
        self.tokenizer = tokenizer_
        self._max_token_id = self.vocab_size - 1

    @classmethod
    def from_pretrained(cls,
                        path_or_repo_id: str,
                        *,
                        revision: Optional[str] = None) -> "MistralTokenizer":
        if not Path(path_or_repo_id).exists():
            assert len(path_or_repo_id.split("/")) == 2, (
                "You have either provided a non-existent path: "
                "{path_or_repo_id} or an invalid HF Hub repo id.")
            tokenizer_file = cls._download_mistral_tokenizer_from_hf(
                path_or_repo_id, revision)
        elif Path(path_or_repo_id).is_dir():
            tokenizer_file_name = find_tokenizer_file(
                os.listdir(path_or_repo_id))
            tokenizer_file = str(Path(path_or_repo_id) / tokenizer_file_name)
        else:
            assert Path(
                path_or_repo_id).is_file(), f"Invalid path: {path_or_repo_id}"
            tokenizer_file = str(Path(path_or_repo_id))

        from mistral_common.tokens.tokenizers.mistral import (
            MistralTokenizer as PublicMistralTokenizer)
        mistral_tokenizer = PublicMistralTokenizer.from_file(tokenizer_file)
        return cls(mistral_tokenizer)

    @staticmethod
    def _download_mistral_tokenizer_from_hf(tokenizer_name: str,
                                            revision: Optional[str]) -> str:
        try:
            hf_api = HfApi()
            files = hf_api.list_repo_files(repo_id=tokenizer_name,
                                           revision=revision)
        except ConnectionError as exc:
            files = list_local_repo_files(repo_id=tokenizer_name,
                                          revision=revision)

            if len(files) == 0:
                raise exc

        filename = find_tokenizer_file(files)

        tokenizer_file = hf_hub_download(tokenizer_name,
                                         filename=filename,
                                         revision=revision)
        return tokenizer_file

    # the following attributes are set to fit vLLM's design and are used
    # by the guided structured output backends.
    @property
    def all_special_tokens_extended(self) -> list[str]:
        from mistral_common.tokens.tokenizers.base import SpecialTokens

        # tekken defines its own extended special tokens list
        if hasattr(self.tokenizer, "SPECIAL_TOKENS"):
            special_tokens = self.tokenizer.SPECIAL_TOKENS
        else:
            special_tokens = list(SpecialTokens)
        return [
            s.value if isinstance(s, SpecialTokens) else s
            for s in special_tokens
        ]

    @property
    def all_special_tokens(self) -> list[str]:
        return self.all_special_tokens_extended

    @property
    def all_special_ids(self) -> list[int]:
        return [
            self.all_special_tokens.index(t) for t in self.all_special_tokens
        ]

    @property
    def bos_token_id(self) -> int:
        return self.tokenizer.bos_id

    @property
    def eos_token_id(self) -> int:
        return self.tokenizer.eos_id

    @property
    def sep_token(self) -> str:
        raise NotImplementedError()

    @property
    def pad_token(self) -> str:
        raise NotImplementedError()

    @property
    def is_fast(self) -> bool:
        return True

    @property
    def vocab_size(self) -> int:
        return len(self._vocab)

    @property
    def max_token_id(self) -> int:
        return self._max_token_id

    def __len__(self) -> int:
        return self.vocab_size

    def __call__(
        self,
        text: Union[str, list[str], list[int]],
        text_pair: Optional[str] = None,
        add_special_tokens: bool = False,
        truncation: bool = False,
        max_length: Optional[int] = None,
    ):
        input_ids: Union[list[int], list[list[int]]]
        # For list[str], original prompt text
        if is_list_of(text, str):
            input_ids_: list[list[int]] = []
            for p in text:
                each_input_ids = self.encode_one(p, truncation, max_length)
                input_ids_.append(each_input_ids)
            input_ids = input_ids_
        # For list[int], apply chat template output, already tokens.
        elif is_list_of(text, int):
            input_ids = text
        # For str, single prompt text
        else:
            input_ids = self.encode_one(text, truncation, max_length)
        return Encoding(input_ids=input_ids)

    def get_vocab(self) -> dict[str, int]:
        # NB: the dictionary form of the vocabulary collapses token ids that map
        # to the same string but have different bytes
        return self._vocab_dict

    def get_added_vocab(self) -> dict[str, int]:
        # Mistral tokenizers have no added vocabulary
        return {}

    def encode_one(
        self,
        text: str,
        truncation: bool = False,
        max_length: Optional[int] = None,
    ) -> list[int]:
        # Mistral Tokenizers should not add special tokens
        input_ids = self.encode(text)

        if truncation:
            input_ids = input_ids[:max_length]
        return input_ids

    def encode(self,
               text: str,
               truncation: Optional[bool] = None,
               max_length: Optional[int] = None,
               add_special_tokens: Optional[bool] = None) -> list[int]:
        # `encode` should only be used for prompt completion
        # it should never be used for chat_completion.
        # For chat completion use `apply_chat_template`
        if add_special_tokens is not None:
            return self.tokenizer.encode(text,
                                         bos=add_special_tokens,
                                         eos=add_special_tokens)
        else:
            return self.tokenizer.encode(text, bos=True, eos=False)

    def apply_chat_template(self,
                            messages: list["ChatCompletionMessageParam"],
                            tools: Optional[list[dict[str, Any]]] = None,
                            **kwargs) -> list[int]:

        request = make_mistral_chat_completion_request(messages, tools)
        encoded = self.mistral.encode_chat_completion(request)

        # encode-decode to get clean prompt
        return encoded.tokens

    def convert_tokens_to_string(self, tokens: list[str]) -> str:
        from mistral_common.tokens.tokenizers.base import SpecialTokens
        if self.is_tekken:
            tokens = [
                t for t in tokens
                if (t is SpecialTokens.tool_calls
                    or t not in self.tokenizer._all_special_tokens)
            ]

            if any(isinstance(t, bytes) for t in tokens):
                # we need to encode and decode all tokens again
                shift = self.tokenizer.num_special_tokens

                def _token_to_id(t: str):
                    t_bytes = t.encode("utf-8") \
                        if not isinstance(t, bytes) else t
                    try:
                        return shift + \
                            self.tokenizer._tekken_token2id_nospecial[t_bytes]
                    except KeyError:
                        logger.warning(
                            "Failed to convert token %s to id,"
                            " replacing with <unk>", t_bytes)
                        return self.tokenizer.unk_id

                ids = [_token_to_id(t) for t in tokens]
                decoded = self.tokenizer.decode(ids)
            else:
                decoded = "".join(tokens)
        else:
            # make sure certain special tokens like Tool calls are
            # not decoded
            special_tokens = {SpecialTokens.tool_calls}
            regular_tokens: list[str] = []
            decoded_list = []

            for token in tokens:
                if token in special_tokens:
                    if regular_tokens:
                        decoded_list.append(
                            self.tokenizer.decode(regular_tokens))
                        regular_tokens = []
                    decoded_list.append(token)
                else:
                    regular_tokens.append(token)

            if regular_tokens:
                decoded_list.append(
                    self.tokenizer.decode(regular_tokens))  # type: ignore

            decoded = ''.join(decoded_list)

        return decoded

    # WARN: Outlines logits processors can overwrite this method.
    # See: guided_decoding/outlines_logits_processors.py::_adapt_tokenizer
    # for more.
    def decode(self,
               ids: Union[list[int], int],
               skip_special_tokens: bool = True) -> str:
        assert (
            skip_special_tokens
        ), "skip_special_tokens=False is not supported for Mistral tokenizers."

        if isinstance(ids, int):
            ids = [ids]
        return self.tokenizer.decode(ids)

    def convert_ids_to_tokens(
        self,
        ids: list[int],
        skip_special_tokens: bool = True,
    ) -> list[str]:
        from mistral_common.tokens.tokenizers.base import SpecialTokens
        from mistral_common.tokens.tokenizers.instruct import (
            InstructTokenizerV13)

        # TODO(Patrick) - potentially allow special tokens to not be skipped
        assert (
            skip_special_tokens
        ), "skip_special_tokens=False is not supported for Mistral tokenizers."

        assert self.is_tekken or self.is_spm, type(self.tokenizer)

        if self.is_tekken:
            # skip special tokens except tool call and think tokens
            non_skip_special_tokens = {
                self.tokenizer.get_control_token(SpecialTokens.tool_calls)
            }
            if isinstance(self.instruct, InstructTokenizerV13):
                if self.instruct.BEGIN_THINK:
                    non_skip_special_tokens.add(self.instruct.BEGIN_THINK)
                if self.instruct.END_THINK:
                    non_skip_special_tokens.add(self.instruct.END_THINK)
            ids = [
                i for i in ids if i > self.tokenizer.num_special_tokens
                or i in non_skip_special_tokens
            ]

        tokens = [self.tokenizer.id_to_piece(id) for id in ids]

        if any("�" in t for t in tokens) and self.is_tekken:
            # if a decoded token contains the replacement character, then the
            # token has an incomplete UTF-8 character so we must use bytes
            # See: https://github.com/vllm-project/vllm/pull/8640
            #      https://github.com/vllm-project/vllm/pull/9625
            # if underlying tokenizeir is sentencepiece, we just add "�"
            tokens = [self.tokenizer.id_to_byte_piece(id) for id in ids]

        return tokens
