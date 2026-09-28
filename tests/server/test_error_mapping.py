# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Focused tests for HTTP server exception-to-error mapping."""

import pytest

from openviking.pyagfs.exceptions import (
    AGFSClientError,
    AGFSDirectoryNotEmptyError,
    AGFSHTTPError,
    AGFSInternalError,
    AGFSIsADirectoryError,
    AGFSNotADirectoryError,
    AGFSNotSupportedError,
    AGFSPluginError,
    AGFSResourceExhaustedError,
    GitConcurrentCommitError,
)
from openviking.server.error_mapping import map_exception
from openviking.server.models import ERROR_CODE_TO_HTTP_STATUS
from openviking.storage.errors import LockAcquisitionError, ResourceBusyError, VikingDBException
from openviking_cli.exceptions import (
    FailedPreconditionError,
    InvalidArgumentError,
    InvalidURIError,
    NotFoundError,
    ResourceExhaustedError,
)


class _UpstreamHTTPError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _HTTPStatusError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.response = _Response(status_code)


def test_agfs_client_does_not_exist_maps_to_not_found():
    mapped = map_exception(
        AGFSClientError("path viking://missing does not exist"),
        resource="viking://missing",
        resource_type="file",
    )

    assert isinstance(mapped, NotFoundError)
    assert mapped.code == "NOT_FOUND"
    assert mapped.details == {"resource": "viking://missing", "type": "file"}


def test_agfs_client_invalid_uri_maps_to_invalid_uri():
    mapped = map_exception(
        AGFSClientError("Invalid URI: viking://"),
        resource="viking://",
    )

    assert isinstance(mapped, InvalidURIError)
    assert mapped.code == "INVALID_URI"
    assert mapped.details["uri"] == "viking://"


def test_agfs_http_status_keeps_storage_mapping():
    mapped = map_exception(
        AGFSHTTPError("No such file or directory", 404),
        resource="viking://missing",
        resource_type="file",
    )

    assert isinstance(mapped, NotFoundError)
    assert mapped.code == "NOT_FOUND"
    assert mapped.message == "File not found: viking://missing"


@pytest.mark.parametrize(
    ("error", "expected_details"),
    [
        (AGFSIsADirectoryError("is a directory"), {"expected": "file", "actual": "directory"}),
        (IsADirectoryError("is a directory"), {"expected": "file", "actual": "directory"}),
        (AGFSNotADirectoryError("not a directory"), {}),
        (NotADirectoryError("not a directory"), {}),
        (AGFSPluginError("plugin error: not a directory: /resources/file.md"), {}),
        (AGFSClientError("not a directory"), {}),
        (ValueError("not a directory"), {}),
        (AGFSDirectoryNotEmptyError("directory not empty"), {}),
    ],
)
def test_file_directory_misuse_maps_to_invalid_argument(error, expected_details):
    mapped = map_exception(
        error,
        resource="viking://resources/docs",
        resource_type="file",
    )

    assert isinstance(mapped, InvalidArgumentError)
    assert mapped.code == "INVALID_ARGUMENT"
    assert ERROR_CODE_TO_HTTP_STATUS[mapped.code] == 400
    assert mapped.details == {
        "resource": "viking://resources/docs",
        **expected_details,
    }


def test_agfs_not_supported_maps_to_unimplemented():
    mapped = map_exception(AGFSNotSupportedError("git feature disabled"))

    assert mapped is not None
    assert mapped.code == "UNIMPLEMENTED"


def test_agfs_resource_limit_maps_to_resource_exhausted():
    mapped = map_exception(
        AGFSResourceExhaustedError("blob exceeds configured limit"),
        resource="viking://user/test/memories/experiences/example.md",
    )

    assert isinstance(mapped, ResourceExhaustedError)
    assert mapped.code == "RESOURCE_EXHAUSTED"


def test_value_error_invalid_uri_maps_to_invalid_uri():
    mapped = map_exception(ValueError("invalid viking URI: missing path"), resource="viking://")

    assert isinstance(mapped, InvalidURIError)
    assert mapped.code == "INVALID_URI"


def test_wrapped_upstream_401_maps_to_unauthenticated():
    try:
        raise RuntimeError("OpenAI VLM completion failed") from _UpstreamHTTPError(
            401, "invalid_api_key"
        )
    except RuntimeError as exc:
        mapped = map_exception(exc)

    assert mapped is not None
    assert mapped.code == "UNAUTHENTICATED"
    assert mapped.details["upstream_status_code"] == 401


def test_upstream_provider_payload_message_is_not_duplicated():
    provider_error = (
        "Error code: 401 - {'error': {'code': 'AuthenticationError', "
        "'message': \"The API key doesn\\'t exist. Request id: req-1\", "
        "'param': '', 'type': 'Unauthorized'}}, request_id: req-1"
    )
    try:
        raise RuntimeError(f"Volcengine embedding failed: {provider_error}") from (
            _UpstreamHTTPError(401, provider_error)
        )
    except RuntimeError as exc:
        mapped = map_exception(exc)

    assert mapped is not None
    assert mapped.code == "UNAUTHENTICATED"
    assert mapped.message == (
        "Upstream model authentication failed (HTTP 401): "
        "The API key doesn't exist. Request id: req-1"
    )
    assert mapped.message.count("The API key doesn't exist") == 1
    assert "Error code: 401" not in mapped.message


def test_upstream_response_429_maps_to_resource_exhausted():
    mapped = map_exception(_HTTPStatusError(429, "LiteLLM embedding failed: Too Many Requests"))

    assert mapped is not None
    assert mapped.code == "RESOURCE_EXHAUSTED"
    assert mapped.details["upstream_status_code"] == 429


def test_upstream_text_status_maps_to_permission_denied():
    mapped = map_exception(RuntimeError("Cohere API error: 403 Forbidden"))

    assert mapped is not None
    assert mapped.code == "PERMISSION_DENIED"
    assert mapped.details["upstream_status_code"] == 403


def test_upstream_502_maps_to_unavailable():
    mapped = map_exception(RuntimeError("Volcengine embedding failed: HTTP 502 Bad Gateway"))

    assert mapped is not None
    assert mapped.code == "UNAVAILABLE"
    assert mapped.details["upstream_status_code"] == 502


def test_model_api_key_configuration_error_maps_to_failed_precondition():
    mapped = map_exception(ValueError("VLM configuration requires 'api_key' to be set"))

    assert isinstance(mapped, FailedPreconditionError)
    assert mapped.code == "FAILED_PRECONDITION"


def test_bare_model_api_key_required_maps_to_failed_precondition():
    mapped = map_exception(ValueError("api_key is required"))

    assert isinstance(mapped, FailedPreconditionError)
    assert mapped.code == "FAILED_PRECONDITION"


def test_resource_busy_maps_to_structured_conflict():
    mapped = map_exception(
        ResourceBusyError(
            "Reexact is busy: viking://resources/docs/a.md",
            uri="viking://resources/docs/a.md",
            conflict_type="path_busy",
            retryable=True,
        ),
        resource="viking://resources/docs",
    )

    assert mapped is not None
    assert mapped.code == "CONFLICT"
    assert mapped.details == {
        "resource": "viking://resources/docs/a.md",
        "uri": "viking://resources/docs/a.md",
        "conflict_type": "path_busy",
        "retryable": True,
    }


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_status"),
    [
        (LockAcquisitionError("Failed to acquire exact lock"), "CONFLICT", 409),
        (AGFSInternalError("invalid lock token: missing ':' in token"), "INTERNAL", 500),
        (AGFSInternalError("lock I/O error: failed to create lock dir"), "INTERNAL", 500),
        (AGFSInternalError("AES-GCM authentication failed: aead::Error"), "INTERNAL", 500),
        (AGFSPluginError("plugin error: backend connection lost"), "UNAVAILABLE", 503),
    ],
)
def test_storage_conflicts_remain_distinct_from_failures(error, expected_code, expected_status):
    mapped = map_exception(
        error,
        resource="viking://resources/docs/a.md",
    )

    assert mapped is not None
    assert mapped.code == expected_code
    assert ERROR_CODE_TO_HTTP_STATUS[mapped.code] == expected_status
    if expected_code == "CONFLICT":
        assert mapped.details == {
            "resource": "viking://resources/docs/a.md",
            "uri": "viking://resources/docs/a.md",
            "conflict_type": "path_busy",
            "retryable": True,
        }


def test_git_concurrent_commit_maps_to_conflict():
    err = GitConcurrentCommitError("ref moved")
    mapped = map_exception(err)
    assert mapped is not None
    assert mapped.code == "CONFLICT"
    assert ERROR_CODE_TO_HTTP_STATUS.get(mapped.code) == 409


def test_git_auth_failure_remains_a_client_error():
    from openviking.server.responses import error_response, response_from_result
    from openviking.utils.git_auth import GIT_AUTH_FAILED, raise_git_auth_error
    from openviking_cli.exceptions import OpenVikingError

    with pytest.raises(OpenVikingError) as caught:
        raise_git_auth_error(b"fatal: Authentication failed")
    error = map_exception(caught.value)
    assert error.code == GIT_AUTH_FAILED
    assert ERROR_CODE_TO_HTTP_STATUS.get(error.code, 500) == 400
    assert error_response(error.code, error.message).status_code == 400
    assert (
        response_from_result(
            {"status": "error", "code": error.code, "errors": [error.message]}
        ).status_code
        == 400
    )


class _VikingDBResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _vikingdb_api_key_error(status_code: int, payload: dict) -> Exception:
    from openviking.storage.vectordb.collection.volcengine_api_key_collection import (
        VolcengineApiKeyCollection,
    )

    return VolcengineApiKeyCollection._build_response_error(
        _VikingDBResponse(status_code, payload), "/api/vikingdb/data/search/vector"
    )


def test_vikingdb_invalid_filter_maps_to_invalid_argument():
    err = _vikingdb_api_key_error(
        400,
        {
            "code": "InvalidParameter",
            "message": "Invalid filter param: filter must contain 'op' key, "
            "actual filter: map[type:resource]",
        },
    )

    mapped = map_exception(err)

    assert isinstance(mapped, InvalidArgumentError)
    assert ERROR_CODE_TO_HTTP_STATUS[mapped.code] == 400
    assert "filter must contain 'op' key" in mapped.message
    assert "/api/vikingdb" not in mapped.message
    assert "/api/vikingdb" not in mapped.details["reason"]
    assert mapped.details["upstream_status_code"] == 400
    assert mapped.details["upstream_code"] == "InvalidParameter"
    assert mapped.details["service"] == "vector store"


def test_vikingdb_error_details_redact_credentials():
    err = _vikingdb_api_key_error(
        400, {"code": "InvalidParameter", "message": "bad request api_key=vk-secret-value"}
    )

    mapped = map_exception(err)

    assert mapped.code == "INVALID_ARGUMENT"
    assert "vk-secret-value" not in mapped.message
    assert "vk-secret-value" not in str(mapped.details)


@pytest.mark.parametrize(
    ("status_code", "expected_code", "expected_http"),
    [
        (422, "INVALID_ARGUMENT", 400),
        (429, "RESOURCE_EXHAUSTED", 429),
        (401, "UNAVAILABLE", 503),
        (403, "UNAVAILABLE", 503),
        (404, "UNAVAILABLE", 503),
        (500, "UNAVAILABLE", 503),
        (502, "UNAVAILABLE", 503),
        (503, "UNAVAILABLE", 503),
    ],
)
def test_vikingdb_http_statuses_map_without_internal_error(
    status_code, expected_code, expected_http
):
    err = _vikingdb_api_key_error(status_code, {"code": "Err", "message": "downstream"})

    mapped = map_exception(err)

    assert mapped.code == expected_code
    assert ERROR_CODE_TO_HTTP_STATUS[mapped.code] == expected_http
    assert mapped.details["upstream_status_code"] == status_code


def test_vikingdb_private_client_error_maps_to_vector_store_error():
    from openviking.storage.vectordb.collection.vikingdb_collection import VikingDBCollection

    err = VikingDBCollection._build_response_error(
        _VikingDBResponse(400, {"code": "InvalidParameter", "message": "bad filter"}),
        "/api/vikingdb/data/search/vector",
    )

    mapped = map_exception(err)

    assert mapped.code == "INVALID_ARGUMENT"
    assert mapped.message == "Vector store rejected the request: bad filter"


def test_vector_store_connection_failure_maps_to_unavailable():
    from openviking.storage.errors import ConnectionError as VectorStoreConnectionError

    err = VectorStoreConnectionError(
        "Request to /api/vikingdb/data/search/vector failed: ConnectTimeout",
        error_type="connection_error",
        retryable=True,
    )

    mapped = map_exception(err)

    assert mapped.code == "UNAVAILABLE"
    assert ERROR_CODE_TO_HTTP_STATUS[mapped.code] == 503
    assert "upstream_status_code" not in mapped.details


def test_vikingdb_error_is_found_through_explicit_cause():
    cause = _vikingdb_api_key_error(400, {"message": "bad filter"})
    try:
        raise RuntimeError("search failed") from cause
    except RuntimeError as wrapped:
        mapped = map_exception(wrapped)

    assert mapped.code == "INVALID_ARGUMENT"


def test_http_collection_client_error_maps_to_invalid_argument(monkeypatch):
    from openviking.storage.vectordb.collection.http_collection import HttpCollection

    class _Response:
        status_code = 400
        text = '{"code": "InvalidParameter", "message": "bad filter"}'

    monkeypatch.setattr(
        "openviking.storage.vectordb.collection.http_collection.requests.post",
        lambda *args, **kwargs: _Response(),
    )
    collection = HttpCollection(meta_data={"ProjectName": "p", "CollectionName": "c"})

    with pytest.raises(VikingDBException) as exc_info:
        collection.search_by_scalar("idx", field="updated_at")

    mapped = map_exception(exc_info.value)
    assert mapped.code == "INVALID_ARGUMENT"
    assert mapped.message == "Vector store rejected the request: bad filter"
    assert mapped.details["upstream_status_code"] == 400
    assert mapped.details["upstream_code"] == "InvalidParameter"
