"""
Uniform API error responses.

DRF's default handler returns several different shapes — ``{"detail": ...}``
for permission errors, a field map for validation errors, and an HTML 500 for
anything unhandled. The frontend then needs a different unwrapping rule per
endpoint. This handler normalises everything to a predictable envelope:

    {"error": "<human readable message>", "detail": {...}, "status": 403}

so the React error handling has exactly one shape to read.
"""
import logging

from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


def _first_message(payload):
    """Pull a readable sentence out of a DRF error payload."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list) and payload:
        return _first_message(payload[0])
    if isinstance(payload, dict):
        # Prefer the keys DRF uses for non-field errors.
        for key in ('detail', 'error', 'non_field_errors'):
            if key in payload:
                return _first_message(payload[key])
        for field, value in payload.items():
            message = _first_message(value)
            if message:
                return f'{field}: {message}'
    return ''


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)

    if response is None:
        # Nothing in DRF recognised this: an unexpected server-side bug.
        # Log the traceback for the developer and return a clean 500 rather
        # than leaking an HTML debug page to the API client.
        view = context.get('view').__class__.__name__ if context.get('view') else 'unknown'
        logger.exception('Unhandled exception in %s', view)
        return Response(
            {
                'error': 'An unexpected server error occurred.',
                'detail': str(exc) if str(exc) else exc.__class__.__name__,
                'status': status.HTTP_500_INTERNAL_SERVER_ERROR,
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, Http404):
        message = 'The requested record was not found.'
    else:
        message = _first_message(response.data) or 'Request could not be completed.'

    original = response.data
    response.data = {
        'error': message,
        'status': response.status_code,
    }
    # Keep the field-level errors so forms can highlight the right input.
    if isinstance(original, dict) and set(original) - {'detail'}:
        response.data['detail'] = original

    return response
