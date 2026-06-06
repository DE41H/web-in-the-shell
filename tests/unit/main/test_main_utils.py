"""Tests for main.py utilities."""

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from main import _handle_api_error
from ai.errors import ErrorCategory


@pytest.mark.asyncio
async def test_handle_api_error_auth():
    display = MagicMock()
    
    # We'll use a generic exception with a message that classify will recognize
    exc = Exception("invalid_api_key")
    
    _handle_api_error(exc, display)
    
    display.set_status.assert_called_with("Failed")
    display.log_error.assert_called_once()
    info = display.log_error.call_args[0][0]
    assert info.category == ErrorCategory.AUTH


@pytest.mark.asyncio
async def test_handle_api_error_rate_limit():
    display = MagicMock()
    
    exc = Exception("rate limit")
    
    _handle_api_error(exc, display)
    
    display.set_status.assert_called_with("Failed")
    display.log_error.assert_called_once()
    info = display.log_error.call_args[0][0]
    assert info.category == ErrorCategory.RATE_LIMIT


@pytest.mark.asyncio
async def test_handle_api_error_timeout():
    display = MagicMock()
    
    exc = asyncio.TimeoutError()
    _handle_api_error(exc, display)
    
    display.set_status.assert_called_with("Failed")
    display.log_error.assert_called_once()
    info = display.log_error.call_args[0][0]
    assert info.category == ErrorCategory.TIMEOUT


@pytest.mark.asyncio
async def test_handle_api_error_network():
    display = MagicMock()
    
    exc = httpx.ConnectError("refused")
    _handle_api_error(exc, display)
    
    display.set_status.assert_called_with("Failed")
    display.log_error.assert_called_once()
    info = display.log_error.call_args[0][0]
    assert info.category == ErrorCategory.NETWORK


@pytest.mark.asyncio
async def test_handle_api_error_unknown():
    display = MagicMock()
    
    exc = ValueError("something weird")
    _handle_api_error(exc, display)
    
    display.set_status.assert_called_with("Failed")
    display.log_error.assert_called_once()
    info = display.log_error.call_args[0][0]
    assert info.category == ErrorCategory.UNKNOWN
