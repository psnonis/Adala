import os
import difflib
import asyncio
import aiohttp
from rich import print

from typing import Optional, Dict, Any, List


def check_if_new_openai_version():
    # check openai package version
    from openai import __version__ as openai_version
    from packaging import version

    if version.parse(openai_version) >= version.parse("1.0.0"):
        return True
    else:
        return False


# if version is higher than 1.0.0, then import OpenAI class
if check_if_new_openai_version():
    from openai import OpenAI, NotFoundError
# otherwise, use old style API
else:
    import openai

    OpenAI = Any

from pydantic import model_validator, field_validator, ValidationInfo, Field
from .base import Runtime, AsyncRuntime
from adala.utils.logs import print_error
from adala.utils.internal_data import InternalDataFrame, InternalSeries
from adala.utils.parse import parse_template, partial_str_format
from adala.utils.matching import match_options
from tenacity import retry, stop_after_attempt, wait_random


@retry(wait=wait_random(min=5, max=10), stop=stop_after_attempt(3))
def chat_completion_call(model, messages):
    return openai.ChatCompletion.create(
        model=model, messages=messages, timeout=120, request_timeout=120
    )


DEFAULT_CREATE_COMPLETION_URL = "https://api.openai.com/v1/chat/completions"


@retry(wait=wait_random(min=5, max=10), stop=stop_after_attempt(3))
async def async_create_completion(
    model: str,
    user_prompt: str,
    index: int = 0,
    system_prompt: str = None,
    instruction_first: bool = True,
    semaphore: asyncio.Semaphore = None,
    max_tokens: int = 1000,
    temperature: float = 0.0,
    session: aiohttp.ClientSession = None,
) -> Dict[str, Any]:
    """
    Async version of create_completion function.

    Args:
        model: OpenAI model name.
        user_prompt: User prompt.
        index: Index of the record (to keep track of the order of the records).
        system_prompt: System prompt.
        instruction_first: Whether to put instructions (i.e. system prompt) first. Defaults to True.
        semaphore: Semaphore to limit number of concurrent requests.
        max_tokens: Maximum number of tokens to generate. Defaults to 1000.
        temperature: Temperature for sampling, between 0 and 1. Higher values means the model will take more risks.
            Try 0.9 for more creative applications, and 0 (argmax sampling) for ones with a well-defined answer.
            Defaults to 0.0.
        session: aiohttp session to use for requests.

    Returns:
        Dict[str, Any]: OpenAI response.
    """
    if not semaphore:
        semaphore = asyncio.Semaphore(1)
    if not session:
        session = aiohttp.ClientSession()
    messages = [{"role": "user", "content": user_prompt}]
    if system_prompt:
        if instruction_first:
            messages.insert(0, {"role": "system", "content": system_prompt})
        else:
            messages[0]["content"] += system_prompt
    async with semaphore:
        async with session.post(
            DEFAULT_CREATE_COMPLETION_URL,
            headers={"Authorization": f'Bearer {os.getenv("OPENAI_API_KEY")}'},
            json={
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        ) as response:
            response_json = await response.json()
            completion_text = response_json["choices"][0]["message"]["content"]
            return {
                "text": completion_text,
                "index": index,
            }


@retry(wait=wait_random(min=5, max=10), stop=stop_after_attempt(3))
def chat_completion_call(model, messages):
    return openai.ChatCompletion.create(
        model=model, messages=messages, timeout=120, request_timeout=120
    )


class OpenAIChatRuntime(Runtime):
    """
    Runtime that uses [OpenAI API](https://openai.com/) and chat completion models to perform the skill.

    Attributes:
        openai_model: OpenAI model name.
        openai_api_key: OpenAI API key. If not provided, will be taken from OPENAI_API_KEY environment variable.
        max_tokens: Maximum number of tokens to generate. Defaults to 1000.
    """

    openai_model: str = Field(alias="model")
    openai_api_key: Optional[str] = Field(
        default=os.getenv("OPENAI_API_KEY"), alias="api_key"
    )
    max_tokens: Optional[int] = 1000
    splitter: Optional[str] = None

    _client: OpenAI = None

    def init_runtime(self) -> "Runtime":
        # check openai package version
        if check_if_new_openai_version():
            if self._client is None:
                self._client = OpenAI(api_key=self.openai_api_key)

            # check model availability
            try:
                self._client.models.retrieve(self.openai_model)
            except NotFoundError:
                raise ValueError(
                    f'Requested model "{self.openai_model}" is not available in your OpenAI account.'
                )
        else:
            # deprecated
            models = openai.Model.list(api_key=self.openai_api_key)
            models = set(model["id"] for model in models["data"])
            if self.openai_model not in models:
                print_error(
                    f'Requested model "{self.openai_model}" is not available in your OpenAI account. '
                    f"Available models are: {models}\n\n"
                    f"Try to change the runtime settings for {self.__class__.__name__}, for example:\n\n"
                    f'{self.__class__.__name__}(..., model="gpt-3.5-turbo")\n\n'
                )
                raise ValueError(
                    f"Requested model {self.openai_model} is not available in your OpenAI account."
                )
        return self

    def execute(self, messages: List):
        """
        Execute OpenAI request given list of messages in OpenAI API format
        """
        if self.verbose:
            print(f"OpenAI request: {messages}")

        if check_if_new_openai_version():
            completion = self._client.chat.completions.create(
                model=self.openai_model, messages=messages
            )
            completion_text = completion.choices[0].message.content
        else:
            # deprecated
            completion = chat_completion_call(self.openai_model, messages)
            completion_text = completion.choices[0]["message"]["content"]

        if self.verbose:
            print(f"OpenAI response: {completion_text}")
        return completion_text

    def record_to_record(
        self,
        record: Dict[str, str],
        input_template: str,
        instructions_template: str,
        output_template: str,
        extra_fields: Optional[Dict[str, str]] = None,
        field_schema: Optional[Dict] = None,
        instructions_first: bool = False,
    ) -> Dict[str, str]:
        """
        Execute OpenAI request given record and templates for input, instructions and output.

        Args:
            record: Record to be used for input, instructions and output templates.
            input_template: Template for input message.
            instructions_template: Template for instructions message.
            output_template: Template for output message.
            extra_fields: Extra fields to be used in templates.
            field_schema: Field schema to be used for parsing templates.
            instructions_first: If True, instructions will be sent before input.

        Returns:
            Dict[str, str]: Output record.
        """

        extra_fields = extra_fields or {}
        field_schema = field_schema or {}

        options = {}
        for field, schema in field_schema.items():
            if schema.get("type") == "array":
                options[field] = schema.get("items", {}).get("enum", [])

        output_fields = parse_template(
            partial_str_format(output_template, **extra_fields), include_texts=True
        )
        system_prompt = instructions_template
        user_prompt = input_template.format(**record, **extra_fields)
        messages = [{"role": "system", "content": system_prompt}]

        outputs = {}
        for output_field in output_fields:
            if output_field["type"] == "text":
                if user_prompt is not None:
                    user_prompt += f"\n{output_field['text']}"
                else:
                    user_prompt = output_field["text"]
            elif output_field["type"] == "var":
                name = output_field["text"]
                messages.append({"role": "user", "content": user_prompt})
                completion_text = self.execute(messages)
                if name in options:
                    completion_text = match_options(completion_text, options[name])
                outputs[name] = completion_text
                messages.append({"role": "assistant", "content": completion_text})
                user_prompt = None

        return outputs


class AsyncOpenAIChatRuntime(AsyncRuntime):
    """
    Runtime that uses [OpenAI API](https://openai.com/) and chat completion models to perform the skill.
    It uses async calls to OpenAI API.

    Attributes:
        openai_model: OpenAI model name.
        openai_api_key: OpenAI API key. If not provided, will be taken from OPENAI_API_KEY environment variable.
        max_tokens: Maximum number of tokens to generate. Defaults to 1000.
        temperature: Temperature for sampling, between 0 and 1. Higher values means the model will take more risks.
            Try 0.9 for more creative applications, and 0 (argmax sampling) for ones with a well-defined answer.
            Defaults to 0.0.

        concurrent_clients: Number of concurrent clients to OpenAI API. More clients means more parallel requests, but
            also more money spent and more chances to hit the rate limit. Defaults to 10.
    """

    openai_model: str = Field(alias="model")
    openai_api_key: Optional[str] = Field(
        default=os.getenv("OPENAI_API_KEY"), alias="api_key"
    )
    max_tokens: Optional[int] = 1000
    temperature: Optional[float] = 0.0
    splitter: Optional[str] = None
    concurrent_clients: Optional[int] = 10

    _client: OpenAI = None

    def init_runtime(self) -> "Runtime":
        # check openai package version
        if check_if_new_openai_version():
            if self._client is None:
                self._client = OpenAI(api_key=self.openai_api_key)

            # check model availability
            try:
                self._client.models.retrieve(self.openai_model)
            except NotFoundError:
                raise ValueError(
                    f'Requested model "{self.openai_model}" is not available in your OpenAI account.'
                )
        else:
            # deprecated
            models = openai.Model.list(api_key=self.openai_api_key)
            models = set(model["id"] for model in models["data"])
            if self.openai_model not in models:
                print_error(
                    f'Requested model "{self.openai_model}" is not available in your OpenAI account. '
                    f"Available models are: {models}\n\n"
                    f"Try to change the runtime settings for {self.__class__.__name__}, for example:\n\n"
                    f'{self.__class__.__name__}(..., model="gpt-3.5-turbo")\n\n'
                )
                raise ValueError(
                    f"Requested model {self.openai_model} is not available in your OpenAI account."
                )
        return self

    async def _async_concurrent_create_completion(
        self, prompts, max_concurrent_requests, instruction_first
    ):
        semaphore = asyncio.Semaphore(max_concurrent_requests)

        async with aiohttp.ClientSession() as session:
            tasks = []
            for prompt in prompts:
                task = asyncio.ensure_future(
                    async_create_completion(
                        user_prompt=prompt["user"],
                        system_prompt=prompt["system"],
                        semaphore=semaphore,
                        session=session,
                        model=self.openai_model,
                        max_tokens=self.max_tokens,
                        temperature=self.temperature,
                        instruction_first=instruction_first,
                        index=prompt["index"],
                    )
                )
                tasks.append(task)
            responses = await asyncio.gather(*tasks)
            return responses

    def _prepare_prompt(
        self,
        row,
        input_template: str,
        instructions_template: str,
        suffix: str,
        extra_fields: dict,
    ) -> Dict[str, str]:
        """Prepare input prompt for OpenAI API from the row of the dataframe"""
        return {
            "index": row.name,
            "system": instructions_template,
            "user": input_template.format(**row, **extra_fields) + suffix,
        }

    async def batch_to_batch(
        self,
        batch: InternalDataFrame,
        input_template: str,
        instructions_template: str,
        output_template: str,
        extra_fields: Optional[Dict[str, str]] = None,
        field_schema: Optional[Dict] = None,
        instructions_first: bool = True,
    ) -> InternalDataFrame:
        """Execute batch of requests with async calls to OpenAI API"""

        extra_fields = extra_fields or {}
        field_schema = field_schema or {}

        options = {}
        for field, schema in field_schema.items():
            if schema.get("type") == "array":
                options[field] = schema.get("items", {}).get("enum", [])

        output_fields = parse_template(
            partial_str_format(output_template, **extra_fields), include_texts=True
        )

        if len(output_fields) > 2:
            raise NotImplementedError("Only one output field is supported")

        suffix = ""
        outputs = []
        for output_field in output_fields:
            if output_field["type"] == "text":
                suffix += output_field["text"]

            elif output_field["type"] == "var":
                name = output_field["text"]
                # prepare prompts
                prompts = batch.apply(
                    lambda row: self._prepare_prompt(
                        row, input_template, instructions_template, suffix, extra_fields
                    ),
                    axis=1,
                ).tolist()

                responses = await self._async_concurrent_create_completion(
                    prompts, self.concurrent_clients, instructions_first
                )

                # parse responses, optionally match it with options
                for response in responses:
                    completion_text = response["text"]
                    if name in options:
                        completion_text = match_options(completion_text, options[name])
                    outputs.append({name: completion_text, "index": response["index"]})

        output_df = InternalDataFrame(outputs).set_index("index")
        # return output dataframe ordered as input batch.index
        return output_df.reindex(batch.index)

    async def record_to_record(
        self,
        record: Dict[str, str],
        input_template: str,
        instructions_template: str,
        output_template: str,
        extra_fields: Optional[Dict[str, Any]] = None,
        field_schema: Optional[Dict] = None,
        instructions_first: bool = True,
    ) -> Dict[str, str]:
        raise NotImplementedError("record_to_record is not implemented")


class OpenAIVisionRuntime(OpenAIChatRuntime):
    """
    Runtime that uses [OpenAI API](https://openai.com/) and vision models to perform the skill.
    Only compatible with OpenAI API version 1.0.0 or higher.
    """

    def record_to_record(
        self,
        record: Dict[str, str],
        input_template: str,
        instructions_template: str,
        output_template: str,
        extra_fields: Optional[Dict[str, str]] = None,
        field_schema: Optional[Dict] = None,
        instructions_first: bool = False,
    ) -> Dict[str, str]:
        """
        Execute OpenAI request given record and templates for input, instructions and output.

        Args:
            record: Record to be used for input, instructions and output templates.
            input_template: Template for input message.
            instructions_template: Template for instructions message.
            output_template: Template for output message.
            extra_fields: Extra fields to be used in templates.
            field_schema: Field jsonschema to be used for parsing templates.
                         Field schema must contain "format": "uri" for image fields. For example:
                            ```json
                            {
                                "image": {
                                    "type": "string",
                                    "format": "uri"
                                }
                            }
                            ```
            instructions_first: If True, instructions will be sent before input.
        """

        if not check_if_new_openai_version():
            raise NotImplementedError(
                f"{self.__class__.__name__} requires OpenAI API version 1.0.0 or higher."
            )

        extra_fields = extra_fields or {}
        field_schema = field_schema or {}

        output_fields = parse_template(
            partial_str_format(output_template, **extra_fields), include_texts=False
        )

        if len(output_fields) > 1:
            raise NotImplementedError(
                f"{self.__class__.__name__} does not support multiple output fields. "
                f"Found: {output_fields}"
            )
        output_field = output_fields[0]
        output_field_name = output_field["text"]

        input_fields = parse_template(input_template)

        # split input template into text and image parts
        input_text = ""
        content = [
            {
                "type": "text",
                "text": instructions_template,
            }
        ]
        for field in input_fields:
            if field["type"] == "text":
                input_text += field["text"]
            elif field["type"] == "var":
                if field["text"] not in field_schema:
                    input_text += record[field["text"]]
                elif field_schema[field["text"]]["type"] == "string":
                    if field_schema[field["text"]].get("format") == "uri":
                        if input_text:
                            content.append({"type": "text", "text": input_text})
                            input_text = ""
                        content.append(
                            {"type": "image_url", "image_url": record[field["text"]]}
                        )
                    else:
                        input_text += record[field["text"]]
                else:
                    raise ValueError(
                        f'Unsupported field type: {field_schema[field["text"]]["type"]}'
                    )
        if input_text:
            content.append({"type": "text", "text": input_text})

        if self.verbose:
            print(f"**Prompt content**:\n{content}")

        completion = self._client.chat.completions.create(
            model=self.openai_model,
            messages=[{"role": "user", "content": content}],
            max_tokens=self.max_tokens,
        )

        completion_text = completion.choices[0].message.content
        return {output_field_name: completion_text}
