"""Connection validation and health checks for :class:`LLMService`."""

import logging

from app.services.bedrock_settings import resolve_region
from app.services.llm_host import LLMHost
from app.services.llm_types import PROVIDERS_REQUIRING_API_KEY
from app.services.llm_types import SDK_PROVIDERS
from app.services.llm_types import LLMProvider

logger = logging.getLogger(__name__)


class LLMHealthMixin(LLMHost):
    """Connection validation and health probes (mixed into ``LLMService``)."""

    def validate_connection(self) -> tuple[bool, str]:
        """
        Validate connection to LLM provider.

        Uses the same endpoint resolution as chat_completion() to ensure
        the test accurately reflects what will happen during actual use.

        Returns:
            Tuple of (success, message)
        """
        # Reject a missing credential locally, before any outbound request. Nothing validated
        # the API key here, so a blank one produced `Authorization: Bearer ` and the provider
        # answered 401 — which meant the admin "test connection" button reported a generic
        # remote failure for a locally-obvious mistake, and the unit suite could only assert
        # it by really calling api.openai.com (issue #431).
        if (
            self.config.provider in PROVIDERS_REQUIRING_API_KEY
            and not (self.config.api_key or "").strip()
        ):
            return False, (
                f"An API key is required for {self.config.provider}. "
                "Add one in Settings before testing the connection."
            )

        if self.config.provider == LLMProvider.BEDROCK:
            # An SDK call with its own failure modes (credentials, model access, region);
            # answered with fixed sentences, never exception text.
            from app.services.llm_bedrock import check_connection

            return check_connection(
                model=self.config.model,
                region=resolve_region(self.config.bedrock),
                connection=self.config.bedrock,
            )

        try:
            headers = self._get_headers()

            # Claude/Anthropic have no models endpoint to probe, so both test with a simple
            # request instead, exercising the exact code path a real chat turn would take.
            if self.config.provider in [
                LLMProvider.CLAUDE,
                LLMProvider.ANTHROPIC,
            ]:
                # No local guard here: this branch reaches the network through
                # `chat_completion`, which now validates and PINS the URL it actually
                # POSTs to (`_endpoint_session`). The `is_safe_url(base_url)` check that
                # used to sit here was validate-only — it judged a *different* string
                # from the one fetched, resolved it a second time, and for Claude/
                # Anthropic judged a `base_url` that is never used at all (their
                # endpoints are fixed). Bedrock has no `base_url` concept whatsoever.
                # The refusal still surfaces here, as the `LLMEndpointBlockedError`
                # message caught below.
                #
                # Test with a simple message
                test_messages = [{"role": "user", "content": "Hi"}]
                response = self.chat_completion(test_messages, max_tokens=5)
                if response and response.content and response.content.strip():
                    return (
                        True,
                        f"Connection successful - Model responded: '{response.content[:50]}'",
                    )
                else:
                    return False, "Connection established but model returned empty response"

            else:
                # For OpenAI-compatible providers, derive models endpoint from chat completions endpoint
                # This ensures we test the same server that chat_completion() will use
                chat_endpoint = self.endpoints.get(self.config.provider)
                if not chat_endpoint:
                    return False, f"No endpoint configured for {self.config.provider}"

                # Derive models endpoint from chat completions endpoint
                # e.g., http://host:8000/v1/chat/completions → http://host:8000/v1/models
                if "/chat/completions" in chat_endpoint:
                    models_url = chat_endpoint.replace("/chat/completions", "/models")
                elif "/api/chat" in chat_endpoint:
                    # Ollama uses /api/chat, models endpoint is /api/tags
                    models_url = chat_endpoint.replace("/api/chat", "/api/tags")
                else:
                    # Fallback: try appending /models to base
                    models_url = chat_endpoint.rsplit("/", 1)[0] + "/models"

                logger.debug(
                    f"Testing connection to {self.config.provider}: {models_url} (derived from {chat_endpoint})"
                )

                # `models_url` is DERIVED from base_url, so the guard above validated a
                # different string than the one fetched. Validate and PIN the actual target:
                # `is_safe_url` resolves, judges, and discards, leaving requests to resolve
                # again at connect time — a rebinding window. `resolve_pinned_target` returns
                # the checked IP to dial plus the hostname to verify TLS against.
                from app.core.config import settings as _settings
                from app.utils.url_validation import pinned_requests_session
                from app.utils.url_validation import resolve_pinned_target

                target, reason = resolve_pinned_target(
                    models_url, allow_private=_settings.LLM_ALLOW_PRIVATE_ENDPOINTS
                )
                if target is None:
                    logger.warning("Connection test blocked for %s: %s", models_url, reason)
                    return False, (
                        "Connection failed: the endpoint must be a publicly reachable "
                        "http(s) address. Set LLM_ALLOW_PRIVATE_ENDPOINTS=true to allow a "
                        "self-hosted endpoint on a private network."
                    )

                # A one-off pinned session for this request: its SNI hostname is bound to
                # this target, so it must not be reused or redirected. It is deliberately
                # not the instance session `_endpoint_session` builds — that one is pinned
                # to the *chat* endpoint, and this is the derived models URL.
                # Losing the retry adapter is correct here — a connection *test* that
                # silently retries a 500 three times is reporting the wrong thing anyway.
                with pinned_requests_session(target) as pinned:
                    http_response = pinned.get(
                        target.url,
                        headers={**headers, **target.headers},
                        timeout=10,
                        allow_redirects=False,
                    )

                if http_response.status_code == 200:
                    return True, f"Connection successful (tested {models_url})"
                else:
                    return (
                        False,
                        f"Connection test failed with status {http_response.status_code} at {models_url}",
                    )

        except Exception as e:
            # validate_connection's other caller (llm_settings.py) dials a
            # caller-supplied endpoint, where the dial result IS the product.
            # But the config here also resolves via create_from_settings, which
            # falls back to the deployment's SYSTEM settings -- so a plain user
            # testing their own config can end up dialing (and seeing the
            # message for) the deployment's own LLM host:port. Only the class
            # of failure is returned; the real cause is still in the log.
            logger.exception("LLM connection test failed")
            return False, f"Connection failed ({type(e).__name__})"

    def health_check(self) -> bool:
        """
        Quick health check using the models endpoint

        Returns:
            True if LLM is available, False otherwise
        """
        if self.config.provider in SDK_PROVIDERS:
            # No HTTP models endpoint and no base_url to build one from, so the probe
            # below would always report "down" (issue #1046) while the tasks, which
            # never call this, run fine. Construction already required what actually
            # gates an SDK call (model ID and region, see `_get_provider_config`) --
            # the same answer `is_configured_for_user` gives.
            return bool(self.config.model)

        try:
            headers = self._get_headers()

            # Build models endpoint URL
            base_url = self.config.base_url.strip().rstrip("/") if self.config.base_url else None
            if not base_url:
                logger.info("Health check failed: No base URL configured")
                return False

            if base_url.endswith("/v1"):
                models_url = f"{base_url}/models"
            else:
                models_url = f"{base_url}/v1/models"

            logger.info(f"Health check using models endpoint: {models_url}")

            # The base URL is user-supplied config; refuse internal targets before
            # fetching (issue #284 A0.1) and PIN the checked address, so the request goes
            # to the address that was judged rather than to whatever the resolver answers
            # a second time (DNS rebinding).
            from app.core.config import settings as _settings
            from app.utils.url_validation import pinned_requests_session
            from app.utils.url_validation import resolve_pinned_target

            target, reason = resolve_pinned_target(
                models_url, allow_private=_settings.LLM_ALLOW_PRIVATE_ENDPOINTS
            )
            if target is None:
                logger.warning("Health check blocked for %s: %s", models_url, reason)
                return False

            # Use a short timeout with no retries for health checks — this must
            # not block a sync thread for 30+ seconds if the LLM server is down.
            # `allow_redirects=False`: the pin covers one hop, and a 302 to an internal
            # address would otherwise be followed with no check at all.
            with pinned_requests_session(target) as _session:
                response = _session.get(
                    target.url,
                    headers={**headers, **target.headers},
                    timeout=3,
                    allow_redirects=False,
                )
            logger.info(f"Health check response status: {response.status_code}")

            if response.status_code == 200:
                # Optionally verify our model is in the list
                try:
                    data = response.json()
                    if "data" in data:
                        model_ids = [model.get("id") for model in data["data"]]
                        if self.config.model in model_ids:
                            logger.info(f"Model {self.config.model} found in available models")
                            return True
                        else:
                            logger.warning(
                                f"Model {self.config.model} not found in available models: {model_ids}"
                            )
                            return False  # Model not available
                    return True
                except Exception as e:
                    logger.debug(f"Could not parse models response, but got 200: {e}")
                    return True  # Service is up even if we can't parse response
            else:
                logger.info(f"Models endpoint returned status {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Health check failed for {self.config.provider}: {e}", exc_info=True)
            return False
