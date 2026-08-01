"""Google and OpenAI image provider adapters."""

from __future__ import annotations

import base64
import binascii
from typing import Any

import httpx

from .models import (
    GeneratedImage,
    ImageProviderError,
    ImageProviderRejected,
    ImageRequest,
    Layout,
)

_GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
_OPENAI_URL = "https://api.openai.com/v1/images/generations"
_GOOGLE_RATIOS: dict[Layout, str] = {
    "portrait": "2:3",
    "square": "1:1",
    "landscape": "3:2",
}
_OPENAI_SIZES: dict[Layout, str] = {
    "portrait": "1024x1536",
    "square": "1024x1024",
    "landscape": "1536x1024",
}


class GoogleImageProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._client = httpx.Client(
            headers={"x-goog-api-key": api_key},
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
        )

    def generate(self, request: ImageRequest) -> GeneratedImage:
        payload = {
            "model": self._model,
            "input": [{"type": "text", "text": request.prompt}],
            "response_format": {
                "type": "image",
                "mime_type": "image/png",
                "aspect_ratio": _GOOGLE_RATIOS[request.layout],
                "image_size": "1K",
            },
            "stream": False,
            "store": False,
        }
        data = _post_json(self._client, _GOOGLE_URL, payload)
        if data.get("status") != "completed":
            raise ImageProviderRejected
        steps = data.get("steps")
        if not isinstance(steps, list):
            raise ImageProviderError
        outputs = [
            step for step in steps if isinstance(step, dict) and step.get("type") == "model_output"
        ]
        if len(outputs) != 1:
            raise ImageProviderRejected
        content = outputs[0].get("content")
        if not isinstance(content, list):
            raise ImageProviderError
        if len(content) != 1:
            raise ImageProviderRejected
        image = content[0]
        if not isinstance(image, dict):
            raise ImageProviderError
        if image.get("type") != "image" or image.get("mime_type") != "image/png":
            raise ImageProviderRejected
        return GeneratedImage(_decode_base64(image.get("data")), "image/png")

    def close(self) -> None:
        self._client.close()


class OpenAIImageProvider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
        )

    def generate(self, request: ImageRequest) -> GeneratedImage:
        payload = {
            "model": self._model,
            "prompt": request.prompt,
            "n": 1,
            "stream": False,
            "output_format": "png",
            "quality": "medium",
            "size": _OPENAI_SIZES[request.layout],
        }
        data = _post_json(self._client, _OPENAI_URL, payload)
        images = data.get("data")
        if not isinstance(images, list):
            raise ImageProviderError
        if len(images) != 1:
            raise ImageProviderRejected
        image = images[0]
        if not isinstance(image, dict):
            raise ImageProviderError
        return GeneratedImage(_decode_base64(image.get("b64_json")), "image/png")

    def close(self) -> None:
        self._client.close()


def _post_json(client: httpx.Client, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = client.post(url, json=payload)
    except httpx.TimeoutException:
        raise
    except httpx.HTTPError as exc:
        raise ImageProviderError from exc
    if response.status_code >= 400:
        raise ImageProviderError
    try:
        data = response.json()
    except ValueError as exc:
        raise ImageProviderError from exc
    if not isinstance(data, dict):
        raise ImageProviderError
    return data


def _decode_base64(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise ImageProviderError
    try:
        content = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ImageProviderError from exc
    if not content:
        raise ImageProviderError
    return content


__all__ = ["GoogleImageProvider", "OpenAIImageProvider"]
