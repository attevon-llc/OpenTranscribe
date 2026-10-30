"""ML Commons model deployment, post-deploy inference verification and ensure-deployed flow.

Mixed into :class:`~app.services.search.ml_model_service.OpenSearchMLModelService`.
"""

import logging
import time

from app.services.search.ml_model_registration import _REGISTRATION_MAX_WAIT
from app.services.search.ml_model_registration import _REGISTRATION_POLL_INTERVAL
from app.services.search.ml_model_registration import MLModelRegistrationMixin

logger = logging.getLogger(__name__)

_DEPLOYMENT_POLL_INTERVAL = 2.0  # seconds
_DEPLOYMENT_MAX_WAIT = 120  # 2 minutes max wait for deployment

#: ML Commons states from which a deploy is legal (issue #625 follow-up). Deploying
#: a model in any OTHER state does not merely fail: OpenSearch's own deploy-failure
#: cleanup runs ``ModelHelper.deleteFileCache(modelId)``, which recursively deletes
#: ``ml_cache/models_cache/register/<modelId>/`` -- the exact directory an in-flight
#: REGISTRATION is still writing its download into. Measured on opensearch:3.4.0: a
#: deploy issued ~3s into a registration that would otherwise COMPLETE in ~12-18s
#: turns it into `REGISTER_MODEL FAILED: <path>.zip (No such file or directory)` plus
#: a `DEPLOY_MODEL` NPE ("totalChunks is null") -- byte-identical to the failure a
#: real fresh-install rehearsal hit live. The control (no concurrent deploy) always
#: completes. This is a genuine race, not a slow host: the failing task went from
#: created to FAILED in ~6 seconds, so no poll duration could have saved it.
#:
#: The race exists because #625 gave neural-search bootstrap TWO independent
#: callers -- the one-shot startup path (``app.startup.warmups._initialize_neural_search``)
#: and the beat self-heal (``app.tasks.search_maintenance_task.neural_search_bootstrap_task``,
#: every 10 minutes) -- and `find_model_by_name` matches a model the instant ML
#: Commons creates its meta document (state REGISTERING), long before it is
#: actually deployable. Whichever caller runs second used to see "registered but
#: not deployed" and deploy into the first caller's in-flight write.
_DEPLOYABLE_STATES = frozenset({"REGISTERED", "DEPLOY_FAILED", "PARTIALLY_DEPLOYED"})

#: States meaning "another actor is mid-registration/deploy right now" -- wait for
#: one of these to resolve, never deploy into it.
_IN_FLIGHT_STATES = frozenset({"REGISTERING", "DEPLOYING"})

#: Probe text for the post-deploy inference check (#503). Ordinary prose, because
#: the point is to exercise the normal path rather than an edge case.
_VERIFICATION_TEXT = "the quarterly planning meeting covered budget and hiring"


class MLModelDeploymentMixin(MLModelRegistrationMixin):
    """Deploy, verify and undeploy models (mixed into ``OpenSearchMLModelService``)."""

    def deploy_model(self, model_id: str) -> bool:
        """Deploy a registered model to make it ready for inference.

        Args:
            model_id: The OpenSearch model ID.

        Returns:
            True if deployment succeeded.
        """
        if not self._ensure_client():
            return False

        try:
            assert self._client is not None
            response = self._client.transport.perform_request(
                "POST",
                f"/_plugins/_ml/models/{model_id}/_deploy",
            )

            task_id = response.get("task_id")
            if not task_id:
                # Some models deploy synchronously
                status = response.get("status", "").upper()
                if status == "DEPLOYED":
                    logger.info(f"Model {model_id} deployed (synchronous)")
                    return True
                logger.warning(f"Deploy response without task_id: {response}")
                return False

            # Wait for deployment
            return self._wait_for_deployment(model_id, task_id)

        except Exception as e:
            logger.error(f"Failed to deploy model {model_id}: {e}")
            return False

    def _wait_for_deployment(
        self,
        model_id: str,
        task_id: str,
        *,
        max_wait: float = _DEPLOYMENT_MAX_WAIT,
        poll_interval: float = _DEPLOYMENT_POLL_INTERVAL,
    ) -> bool:
        """Wait for model deployment to complete.

        Args:
            model_id: The model ID.
            task_id: The deployment task ID.
            max_wait: Ceiling in seconds. Defaults to the module constant; only
                overridden by tests so they don't pay the real ceiling.
            poll_interval: Seconds between polls. Same override rationale as `max_wait`.

        Returns:
            True if deployment succeeded.
        """
        start_time = time.time()

        while (time.time() - start_time) < max_wait:
            try:
                # Check task status
                assert self._client is not None
                task_response = self._client.transport.perform_request(
                    "GET",
                    f"/_plugins/_ml/tasks/{task_id}",
                )

                state = task_response.get("state", "").upper()

                if state == "COMPLETED":
                    logger.info(f"Model {model_id} deployed successfully")
                    return True

                if state == "FAILED":
                    error = task_response.get("error", "Unknown error")
                    logger.error(f"Model deployment failed: {error}")
                    return False

                logger.debug(f"Deployment task {task_id} state: {state}")
                time.sleep(poll_interval)

            except Exception as e:
                logger.warning(f"Error polling deployment task: {e}")
                time.sleep(poll_interval)

        logger.error(f"Model deployment timed out after {max_wait}s")
        return False

    def _verified_or_none(self, model_name: str, model_id: str) -> str | None:
        """Return the id only if the model can ACTUALLY embed (#503).

        Every return path of :meth:`ensure_model_deployed` goes through here, because a
        model that reports deployed and cannot embed is worse than one that failed to
        deploy: the caller stores it as the active model, the ingest pipeline points at
        it, and every document indexed afterwards silently carries no vector.

        Refusing the id is the loud outcome — the caller already logs
        "Could not deploy default model" and neural search stays off, which is a state
        an operator can see and act on.
        """
        from app.services.search.settings_service import get_search_embedding_dimension

        try:
            expected = get_search_embedding_dimension()
        except Exception:  # noqa: BLE001 - a settings read must not break deployment
            expected = None

        ok, detail = self.verify_model_can_embed(model_id, expected_dimension=expected)
        if ok:
            logger.info(f"Verified {model_name} can embed: {detail}")
            return model_id

        logger.error(
            f"Model {model_name} ({model_id}) reports deployed but CANNOT EMBED: {detail}. "
            "Refusing to activate it. A model that deploys and cannot embed leaves every "
            "later document without a vector and no error anywhere. If the cluster is "
            "small, note the measured floors: 1 GB heap runs all-MiniLM-L6-v2, 2 GB runs "
            "every English model (see docker-compose.yml)."
        )
        return None

    def verify_model_can_embed(
        self, model_id: str, expected_dimension: int | None = None
    ) -> tuple[bool, str]:
        """Run a REAL prediction and check the vector that comes back (#503).

        ⚠️ **"Deployed" is not "working", and the difference is measured.**
        ``all-mpnet-base-v2`` on a 1 GB heap reports ``DEPLOY: COMPLETED`` and then
        fails to produce an embedding — so :meth:`_wait_for_deployment`, which returns
        True on that state, reported a cluster that could not embed as healthy. Every
        document indexed afterwards silently gets no vector, and neural search quietly
        degrades to BM25 with nothing in the logs saying so.

        This is also the cheapest possible check for "does this model exist and work
        here at all", which is the #504 failure (a model offered in nine files that
        OpenSearch does not provide) caught one layer earlier.

        Args:
            model_id: The deployed ML Commons model id.
            expected_dimension: Vector width the index is configured for. When given, a
                mismatch is a failure — writing 768-d vectors into a 384-d ``knn_vector``
                is rejected per document, which surfaces as an empty index rather than
                an error anyone reads.

        Returns:
            ``(ok, detail)``. ``detail`` names the cause on failure, for the log line.
        """
        if not self._ensure_client():
            return False, "OpenSearch client unavailable"

        try:
            assert self._client is not None
            response = self._client.transport.perform_request(
                "POST",
                f"/_plugins/_ml/_predict/text_embedding/{model_id}",
                body={
                    "text_docs": [_VERIFICATION_TEXT],
                    "return_number": True,
                    "target_response": ["sentence_embedding"],
                },
            )
        except Exception as exc:  # noqa: BLE001 - any failure means "cannot embed"
            return False, f"prediction request failed: {exc}"

        try:
            vector = response["inference_results"][0]["output"][0]["data"]
        except (KeyError, IndexError, TypeError):
            return False, f"prediction returned no embedding: {str(response)[:200]}"

        if not vector:
            return False, "prediction returned an empty vector"

        if expected_dimension is not None and len(vector) != expected_dimension:
            return False, (
                f"model returns {len(vector)}-dimension vectors but the index is "
                f"configured for {expected_dimension}. Writing these would be rejected "
                "per document and the index would simply stay empty."
            )

        return True, f"{len(vector)}-dimension embedding"

    def undeploy_model(self, model_id: str) -> bool:
        """Undeploy a model from memory.

        Args:
            model_id: The OpenSearch model ID.

        Returns:
            True if undeploy succeeded.
        """
        if not self._ensure_client():
            return False

        try:
            assert self._client is not None
            self._client.transport.perform_request(
                "POST",
                f"/_plugins/_ml/models/{model_id}/_undeploy",
            )
            logger.info(f"Model {model_id} undeployed")
            return True

        except Exception as e:
            logger.error(f"Failed to undeploy model {model_id}: {e}")
            return False

    def _await_deployable_state(
        self,
        model_id: str,
        *,
        max_wait: float = _REGISTRATION_MAX_WAIT,
        poll_interval: float = _REGISTRATION_POLL_INTERVAL,
    ) -> str:
        """Block while another actor is still registering/deploying this model.

        Returns the model's state once it leaves ``_IN_FLIGHT_STATES`` (or the
        last-seen state if ``max_wait`` elapses first). Callers must never deploy
        into an in-flight state -- see ``_DEPLOYABLE_STATES``'s docstring for the
        destructive OpenSearch cleanup that follows.

        ``max_wait``/``poll_interval`` default to the same module constants
        ``_wait_for_registration`` uses, and exist for the same reason: tests
        override them so this never pays the real ~300s ceiling.
        """
        deadline = time.time() + max_wait
        state = str(self.get_model_status(model_id).get("state", "")).upper()
        while state in _IN_FLIGHT_STATES and time.time() < deadline:
            logger.info(
                f"Model {model_id} is {state}; waiting for it to finish rather than "
                "deploying into it"
            )
            time.sleep(poll_interval)
            state = str(self.get_model_status(model_id).get("state", "")).upper()
        return state

    def ensure_model_deployed(self, model_name: str) -> str | None:
        """Ensure a model is registered and deployed.

        For offline/air-gapped deployments, tries to register from local files first.
        Falls back to remote registration (HuggingFace) if local file not available.

        Args:
            model_name: HuggingFace model name.

        Returns:
            Model ID if successful, None on error.
        """
        # Check if already registered
        model_id = self.find_model_by_name(model_name)

        if model_id:
            status = self.get_model_status(model_id)
            if status.get("deployed"):
                logger.info(f"Model {model_name} already deployed: {model_id}")
                return self._verified_or_none(model_name, model_id)

            # `find_model_by_name` matches the moment ML Commons creates the model's
            # meta document -- state REGISTERING, long before there is anything to
            # deploy. Wait it out rather than deploying into it (see
            # _DEPLOYABLE_STATES's docstring for why that is destructive, not just
            # wrong).
            state = self._await_deployable_state(model_id)
            if state == "DEPLOYED":
                # Finished registering AND deploying while we waited.
                return self._verified_or_none(model_name, model_id)
            if state not in _DEPLOYABLE_STATES:
                logger.warning(
                    f"Model {model_name} ({model_id}) is in state {state!r}, which is "
                    "not deployable. Not deploying -- doing so would delete the "
                    "registration cache of an in-flight register. The bootstrap "
                    "self-heal will retry."
                )
                return None

            # Deploy if registered but not deployed
            if self.deploy_model(model_id):
                return self._verified_or_none(model_name, model_id)
            return None

        # Not registered - try local first (for offline deployments)
        local_path = self.get_local_model_path(model_name)
        if local_path:
            logger.info(f"Registering model from local file: {model_name}")
            model_id = self.register_model_from_local(model_name)
            if model_id and self.deploy_model(model_id):
                return self._verified_or_none(model_name, model_id)
            # Local registration failed, continue to try remote
            logger.warning(f"Local model registration failed for {model_name}, trying remote")

        # Try remote registration (requires internet)
        logger.info(f"Registering model from remote: {model_name}")
        model_id = self.register_model(model_name)
        if not model_id:
            return None

        if self.deploy_model(model_id):
            return self._verified_or_none(model_name, model_id)

        return None
