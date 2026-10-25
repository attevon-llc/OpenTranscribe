"""Privacy policy layer: who may have voiceprints and inferred gender (issues #1280, #1279).

``policy`` resolves the restrict-only layers (platform, instance, organization, user) and
exposes the gates the pipeline, the read paths and the OpenSearch write backstop call.
``consent`` is the extension seam a consent implementation registers a hook on.
"""
