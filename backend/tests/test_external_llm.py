"""
Tests for external LLM connections and edge cases

This module tests various external LLM configurations to ensure
robust handling of different scenarios including:
- External Ollama instances
- External vLLM deployments
- Network failures and timeouts
- Invalid credentials
- Rate limiting scenarios

NOTE: LLMService methods are synchronous (use requests library).
Tests are written as synchronous functions accordingly.
"""

import contextlib
import logging
import socket
import time
from unittest.mock import patch

import pytest

from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)


class TestExternalOllamaConnections:
    """Test external Ollama instance connections"""

    def test_local_ollama_default_config(self):
        """Test connection to default local Ollama setup"""
        config = LLMConfig(
            provider=LLMProvider.OLLAMA,
            model="llama2:7b-chat",
            base_url="http://localhost:11434",
        )

        service = LLMService(config)

        try:
            # This test will only pass if Ollama is actually running
            # In CI/CD, this should be mocked or skipped
            success, message = service.validate_connection()

            # If Ollama is running, we should get a successful connection
            if success:
                assert "successful" in message.lower() or "available" in message.lower()
            else:
                # If not running, we should get a clear error message
                assert (
                    "connection" in message.lower()
                    or "refused" in message.lower()
                    or "failed" in message.lower()
                )

        except Exception as e:
            # Expected if Ollama is not running
            assert "connection" in str(e).lower() or "refused" in str(e).lower()
        finally:
            service.close()

    def test_external_ollama_custom_port(self):
        """Test connection to Ollama on custom port"""
        config = LLMConfig(
            provider=LLMProvider.OLLAMA,
            model="mistral:7b",
            base_url="http://localhost:11435",  # Non-standard port
        )

        service = LLMService(config)

        try:
            success, message = service.validate_connection()
            # This should typically fail unless user has Ollama on port 11435
            assert isinstance(success, bool)
            assert isinstance(message, str)
        except Exception as e:
            # Expected for non-existent service
            assert "connection" in str(e).lower() or "timeout" in str(e).lower()
        finally:
            service.close()

    def test_private_endpoint_is_refused_before_any_request(self):
        """A private base_url is refused by the SSRF guard, with the actionable message.

        This was ``test_ollama_invalid_model``, and it never tested an invalid model. The URL
        is ``http://localhost:11434``, which ``is_safe_url`` rejects as a private endpoint
        unless ``LLM_ALLOW_PRIVATE_ENDPOINTS=true`` (issue #284 A0.1) — so the call never
        reached Ollama. It then asserted only ``if not success:`` against a six-keyword
        ``any(...)`` list containing "failed" and "connection", which the SSRF message happens
        to satisfy ("Connection failed: ..."). It passed by coincidence, for the wrong reason,
        and would have passed just as well had the guard been removed and a real connection
        error occurred instead (issue #431).

        Whether a non-existent *model* is reported usefully is the remote's judgement and
        belongs against the mock LLM server, not a real Ollama that may not be running.
        """
        config = LLMConfig(
            provider=LLMProvider.OLLAMA,
            model="nonexistent-model:latest",
            base_url="http://localhost:11434",
        )
        service = LLMService(config)
        try:
            success, message = service.validate_connection()
        finally:
            service.close()

        assert success is False
        assert "publicly reachable" in message
        assert "LLM_ALLOW_PRIVATE_ENDPOINTS" in message


class TestExternalVLLMConnections:
    """Test external vLLM instance connections"""

    def test_local_vllm_default_config(self):
        """Test connection to default local vLLM setup"""
        config = LLMConfig(
            provider=LLMProvider.VLLM,
            model="microsoft/DialoGPT-medium",
            base_url="http://localhost:8012/v1",
        )

        service = LLMService(config)

        try:
            success, message = service.validate_connection()

            # Log result for debugging
            print(f"vLLM connection test: success={success}, message={message}")

            assert isinstance(success, bool)
            assert isinstance(message, str)
        except Exception as e:
            # Expected if vLLM is not running
            print(f"vLLM connection error (expected): {e}")
        finally:
            service.close()

    def test_external_vllm_custom_endpoint(self):
        """Test connection to external vLLM endpoint"""
        config = LLMConfig(
            provider=LLMProvider.VLLM,
            model="custom-model",
            base_url="http://192.168.1.100:8000/v1",  # Example external IP
        )

        service = LLMService(config)

        try:
            success, message = service.validate_connection()
            assert isinstance(success, bool)
            assert isinstance(message, str)
        except Exception as e:
            # Expected for non-existent external service
            assert any(
                keyword in str(e).lower()
                for keyword in ["connection", "timeout", "unreachable", "refused"]
            )
        finally:
            service.close()

    def test_vllm_with_api_key(self):
        """Test vLLM with API key authentication"""
        config = LLMConfig(
            provider=LLMProvider.VLLM,
            model="test-model",
            base_url="http://localhost:8012/v1",
            api_key="test-api-key-123",
        )

        service = LLMService(config)

        try:
            success, message = service.validate_connection()

            # Should handle API key properly (even if service is down)
            assert isinstance(success, bool)
            assert isinstance(message, str)
        except Exception as e:
            # Connection error is expected if service isn't running
            logger.debug(f"Expected connection error during test: {e}")
        finally:
            service.close()


class TestNetworkEdgeCases:
    """Test network-related edge cases and error handling"""

    def test_connection_timeout(self):
        """Test connection timeout handling"""
        config = LLMConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-3.5-turbo",
            base_url="http://10.255.255.1",  # Non-routable address
            api_key="test-key",
            # Very short timeout
        )

        service = LLMService(config)

        start_time = time.time()
        try:
            success, message = service.validate_connection()
            elapsed = time.time() - start_time

            # Service uses retries (3 retries x 10s timeout = ~40s max + overhead)
            assert elapsed < 60  # Should complete within a minute
            assert success is False
            assert any(
                keyword in message.lower() for keyword in ["timeout", "connection", "failed"]
            )
        except Exception:
            elapsed = time.time() - start_time
            assert elapsed < 60
        finally:
            service.close()

    def test_invalid_url_format(self):
        """Test handling of invalid URL formats"""
        invalid_urls = [
            "not-a-url",
            "ftp://invalid-protocol.com",
            "http://",
            "http://invalid-domain-.com",
            "http://localhost:99999",  # Invalid port
        ]

        for url in invalid_urls:
            config = LLMConfig(
                provider=LLMProvider.VLLM,
                model="test-model",
                base_url=url,
            )

            service = LLMService(config)

            try:
                success, message = service.validate_connection()

                # Should gracefully handle invalid URLs
                assert success is False
                assert len(message) > 0
            except Exception as e:
                # Also acceptable - should catch and handle gracefully
                assert len(str(e)) > 0
            finally:
                service.close()

    def test_dns_resolution_failure(self):
        """Test DNS resolution failure handling"""
        config = LLMConfig(
            provider=LLMProvider.OLLAMA,
            model="test-model",
            base_url="http://nonexistent-domain-12345.com",
        )

        service = LLMService(config)

        try:
            success, message = service.validate_connection()

            assert success is False
            assert any(
                keyword in message.lower()
                for keyword in ["resolve", "dns", "connection", "failed", "unreachable"]
            )
        except Exception as e:
            # Expected for DNS failures
            assert any(keyword in str(e).lower() for keyword in ["resolve", "dns", "connection"])
        finally:
            service.close()


class TestAPIKeyValidation:
    """Test API key validation and authentication edge cases"""

    def test_invalid_openai_api_key(self):
        """Test OpenAI with invalid API key"""
        config = LLMConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-3.5-turbo",
            base_url="https://api.openai.com/v1",
            api_key="invalid-key-123",
        )

        service = LLMService(config)

        try:
            success, message = service.validate_connection()

            # Should detect invalid API key
            if not success:
                assert any(
                    keyword in message.lower()
                    for keyword in [
                        "unauthorized",
                        "invalid",
                        "key",
                        "authentication",
                        "401",
                        "failed",
                    ]
                )
        except Exception as e:
            # May throw exception for auth errors
            assert any(
                keyword in str(e).lower() for keyword in ["unauthorized", "authentication", "key"]
            )
        finally:
            service.close()

    def test_empty_api_key_when_required(self):
        """Test providers that require API keys with empty keys"""
        providers_requiring_keys = [
            (LLMProvider.OPENAI, "gpt-3.5-turbo", "https://api.openai.com/v1"),
            (
                LLMProvider.CLAUDE,
                "claude-3-haiku-20240307",
                "https://api.anthropic.com/v1",
            ),
        ]

        for provider, model, base_url in providers_requiring_keys:
            config = LLMConfig(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key=None,  # No API key provided
            )

            service = LLMService(config)

            try:
                success, message = service.validate_connection()

                # Should fail due to missing API key
                assert success is False
                assert any(
                    keyword in message.lower()
                    for keyword in [
                        "key",
                        "authentication",
                        "required",
                        "missing",
                        "unauthorized",
                        "failed",
                    ]
                )
            except Exception as e:
                # Also acceptable - may throw auth exception
                logger.debug(f"Expected auth exception during test: {e}")
            finally:
                service.close()

    @pytest.mark.parametrize("blank_key", ["", " ", "\t", "   \n "])
    def test_blank_api_key_is_rejected_without_a_network_call(self, blank_key):
        """A provider that needs a key refuses locally, before any outbound request.

        This replaces a test that looped over "malformed" keys, really called
        **api.openai.com** for each, swallowed every exception into `logger.debug`, and
        asserted only `if not success: assert len(message) > 0`. So it could not fail, and it
        put a live third-party network dependency in the ungated unit suite (issue #431).

        A blank key is the case that IS decidable locally, and `validate_connection` now
        decides it — no socket is opened, so this is deterministic and offline. Whether a
        well-formed-but-wrong key is accepted is the remote's judgement, not ours, and is
        covered against the mock LLM server rather than by calling a vendor.
        """
        config = LLMConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-3.5-turbo",
            base_url="https://api.openai.com/v1",
            api_key=blank_key,
        )
        service = LLMService(config)
        try:
            # COUNT lookups; never raise inside the stub. TWO earlier versions of this
            # guard were silently vacuous: patching `service.session.get` (that object has
            # not been on this code path since the SSRF pinning work), and raising
            # AssertionError from a getaddrinfo stub (validate_connection wraps its work in
            # try/except and returns (False, message), so the assertion was SWALLOWED and
            # the test passed either way). A counter asserted AFTER the call survives both.
            lookups: list[tuple] = []
            real_getaddrinfo = socket.getaddrinfo

            def _record(*args, **kwargs):
                lookups.append(args)
                return real_getaddrinfo(*args, **kwargs)

            with patch.object(socket, "getaddrinfo", _record):
                success, message = service.validate_connection()

            assert lookups == [], (
                f"validate_connection resolved {lookups} with a blank API key — it must "
                "make no network request at all"
            )
        finally:
            service.close()

        assert success is False
        assert "API key is required" in message


class TestConnectionTestAPI:
    """Test the connection testing API endpoint via LLMService directly"""

    def test_connection_test_ollama(self):
        """Test connection test with Ollama configuration"""
        config = LLMConfig(
            provider=LLMProvider.OLLAMA,
            model="llama2:7b-chat",
            base_url="http://localhost:11434",
        )

        service = LLMService(config)
        try:
            success, message = service.validate_connection()

            assert isinstance(success, bool)
            assert isinstance(message, str)
            assert len(message) > 0
        finally:
            service.close()

    def test_connection_test_vllm(self):
        """Test connection test with vLLM configuration"""
        config = LLMConfig(
            provider=LLMProvider.VLLM,
            model="microsoft/DialoGPT-medium",
            base_url="http://localhost:8012/v1",
        )

        service = LLMService(config)
        try:
            success, message = service.validate_connection()

            assert isinstance(success, bool)
            assert isinstance(message, str)
            assert len(message) > 0
        finally:
            service.close()


class TestResourceManagement:
    """Test proper resource management and cleanup"""

    def test_service_cleanup_after_failure(self):
        """Test that services are properly cleaned up after connection failures"""
        config = LLMConfig(
            provider=LLMProvider.VLLM,
            model="test-model",
            base_url="http://localhost:9999",  # Unlikely to be in use
        )

        service = LLMService(config)

        with contextlib.suppress(Exception):
            # This should fail
            service.validate_connection()

        # Cleanup should work without errors - this is the main assertion
        # close() should not raise any exceptions
        try:
            service.close()
            cleanup_success = True
        except Exception:
            cleanup_success = False

        assert cleanup_success, "Service cleanup should not raise exceptions"

    def test_multiple_sequential_connections(self):
        """Test handling of multiple sequential connection attempts"""
        configs = [
            LLMConfig(
                provider=LLMProvider.OLLAMA,
                model=f"model-{i}",
                base_url="http://localhost:11434",
            )
            for i in range(5)
        ]

        services = [LLMService(config) for config in configs]

        try:
            # Attempt connections sequentially (sync methods can't use asyncio.gather)
            results: list[tuple[bool, str] | Exception] = []
            for service in services:
                try:
                    result = service.validate_connection()
                    results.append(result)
                except Exception as e:
                    results.append(e)  # type: ignore[arg-type]

            # Should handle all requests without crashing
            assert len(results) == 5

            for result in results:  # type: ignore[assignment]
                if isinstance(result, Exception):
                    # Exceptions are acceptable for failed connections
                    continue
                # At this point, result must be tuple[bool, str]
                if not isinstance(result, tuple):  # type: ignore[misc]
                    continue
                success, message = result  # type: ignore[misc]
                assert isinstance(success, bool)
                assert isinstance(message, str)
        finally:
            # Cleanup all services
            for service in services:
                service.close()


class TestRealWorldScenarios:
    """Test real-world deployment scenarios"""

    def test_user_has_ollama_running_locally(self):
        """Test scenario where user has Ollama running locally"""
        # This test simulates a user who has Ollama installed and running
        config = LLMConfig(
            provider=LLMProvider.OLLAMA,
            model="llama2:7b-chat",
            base_url="http://localhost:11434",
        )

        service = LLMService(config)

        try:
            success, message = service.validate_connection()

            print(f"Local Ollama test: success={success}, message={message}")

            if success:
                # If user actually has Ollama running, test should pass
                assert "successful" in message.lower() or "tested" in message.lower()
            else:
                # If Ollama is not running, should get clear error message
                assert len(message) > 10  # Should be descriptive

        except Exception as e:
            # Expected if Ollama is not installed/running
            print(f"Ollama not available (expected in test environment): {e}")
        finally:
            service.close()

    def test_user_has_external_vllm_server(self):
        """Test scenario where user has external vLLM server"""
        # Simulate external vLLM server (will likely fail in test environment)
        config = LLMConfig(
            provider=LLMProvider.VLLM,
            model="huggingface-model",
            base_url="http://192.168.1.100:8000/v1",
            api_key="optional-api-key",
        )

        service = LLMService(config)

        try:
            success, message = service.validate_connection()

            print(f"External vLLM test: success={success}, message={message}")

            # In test environment, this will likely fail
            # But should fail gracefully with useful message
            assert isinstance(success, bool)
            assert len(message) > 5

            if not success:
                # Should indicate network/connection issue
                assert any(
                    keyword in message.lower()
                    for keyword in ["connection", "timeout", "refused", "unreachable", "failed"]
                )
        except Exception as e:
            print(f"External vLLM connection failed (expected): {e}")
        finally:
            service.close()

    def test_openai_with_custom_base_url_for_vllm(self):
        """Test OpenAI provider configured to point to vLLM server (Issue #100 scenario)"""
        # This tests the bug fix for GitHub Issue #100
        # User configures OpenAI provider but points to their vLLM server
        config = LLMConfig(
            provider=LLMProvider.OPENAI,
            model="Qwen/Qwen2.5-32B-Instruct-AWQ",
            base_url="http://192.168.1.100:8000/v1",  # Custom vLLM endpoint
            api_key="dummy-key",
        )

        service = LLMService(config)

        try:
            # Verify the endpoint is correctly built from custom base_url
            expected_endpoint = "http://192.168.1.100:8000/v1/chat/completions"
            actual_endpoint = service.endpoints[LLMProvider.OPENAI]

            assert actual_endpoint == expected_endpoint, (
                f"OpenAI endpoint should use custom base_url. "
                f"Expected: {expected_endpoint}, Got: {actual_endpoint}"
            )

            # The actual connection will fail (no server), but endpoint should be correct
            success, message = service.validate_connection()

            # Should fail due to connection refused, not because it went to api.openai.com
            assert isinstance(success, bool)
            assert isinstance(message, str)

        except Exception as e:
            print(f"Expected connection failure: {e}")
        finally:
            service.close()


def run_connection_tests():
    """
    Utility function to run connection tests manually

    This can be used to test actual connections when the services are available.
    """
    print("Testing external LLM connections...")

    # Test local Ollama
    print("\n1. Testing local Ollama...")
    test_ollama = TestExternalOllamaConnections()
    test_ollama.test_local_ollama_default_config()

    # Test local vLLM
    print("\n2. Testing local vLLM...")
    test_vllm = TestExternalVLLMConnections()
    test_vllm.test_local_vllm_default_config()

    # Test network edge cases
    print("\n3. Testing network edge cases...")
    test_network = TestNetworkEdgeCases()
    test_network.test_connection_timeout()

    # Test Issue #100 fix
    print("\n4. Testing Issue #100 fix (OpenAI with custom base_url)...")
    test_scenarios = TestRealWorldScenarios()
    test_scenarios.test_openai_with_custom_base_url_for_vllm()

    print("\nConnection tests completed!")


if __name__ == "__main__":
    # Run connection tests if called directly
    run_connection_tests()
