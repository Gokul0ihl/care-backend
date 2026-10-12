"""
Lets plugs change how a patient is shown, for example to mask a protected
patient's identity in every response that carries the patient.

A hook is a callable ``hook(mapping, patient, user)`` that edits the
serialized mapping in place. ``user`` is the user making the current request,
or None outside a request (Celery tasks, management commands).
"""

from contextvars import ContextVar

from django.contrib.auth.models import AnonymousUser

patient_serialization_hooks = []
patient_phone_hooks = []


def patient_phone_allowed(phone):
    return all(hook(phone) for hook in patient_phone_hooks)


_current_request = ContextVar("care_current_request", default=None)


class CurrentRequestMiddleware:
    """Keeps the request for the hooks. DRF authenticates inside the view and
    sets the user on the Django request, so the user is read lazily."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _current_request.set(request)
        try:
            return self.get_response(request)
        finally:
            _current_request.reset(token)


def current_request():
    return _current_request.get()


def current_user():
    request = _current_request.get()
    user = getattr(request, "user", None) if request else None
    if user is None or isinstance(user, AnonymousUser):
        return None
    return user


def run_patient_serialization_hooks(mapping, patient):
    if not patient_serialization_hooks:
        return
    user = current_user()
    for hook in patient_serialization_hooks:
        hook(mapping, patient, user)
