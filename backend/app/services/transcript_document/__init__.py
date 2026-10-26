"""DOCX and PDF transcript documents (issue #1278).

``blocks`` turns masked turns and comments into renderer-neutral blocks, ``layout`` paginates
them into a fixed grid of numbered lines, and ``docx_renderer`` / ``pdf_renderer`` write the
bytes. See ``CLAUDE.md`` in this directory for the library and licence decisions.
"""
