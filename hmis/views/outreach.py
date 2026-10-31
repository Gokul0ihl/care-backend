"""Outreach events (the main app) and camp mode. The rules are in hmis.outreach."""

from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.utils.shortcuts import get_object_or_404
from hmis import outreach
from hmis.models import (
    CampParticipant,
    CampSession,
    OutreachEvent,
    OutreachFormTemplate,
)
from hmis.views.base import EmergencyAPIView, get_facility

MANAGE = "can_manage_outreach_event"
APPROVE = "can_approve_outreach_event"
CAPTURE = "can_capture_camp_participant"


def can_view_events(user, facility):
    return outreach.has_permission(user, facility, MANAGE) or outreach.has_permission(
        user, facility, APPROVE
    )


def get_event(event_id, user):
    event = get_object_or_404(
        OutreachEvent.objects.select_related("facility"),
        external_id=event_id,
        deleted=False,
    )
    if not can_view_events(user, event.facility):
        raise PermissionDenied("You do not have access to this facility's events")
    return event


class OutreachMeView(EmergencyAPIView):
    """What the signed-in user can do with outreach, per facility."""

    def get(self, request):
        return Response(outreach.capabilities(request.user))


class OutreachEventView(EmergencyAPIView):
    def get(self, request):
        facility = get_facility(request.query_params.get("facility"))
        if not can_view_events(request.user, facility):
            raise PermissionDenied("You do not have access to this facility's events")
        events = OutreachEvent.objects.filter(
            facility=facility, deleted=False
        ).select_related("facility")
        wanted = request.query_params.get("status")
        if wanted:
            events = events.filter(status__in=wanted.split(","))
        kind = request.query_params.get("event_type")
        if kind:
            events = events.filter(event_type=kind)
        events = events.order_by("-event_date", "-created_date")[:500]
        counts = outreach.participant_counts(events)
        return Response(
            {
                "results": [
                    outreach.event_summary(e, counts.get(e.id, 0)) for e in events
                ]
            }
        )

    def post(self, request):
        spec = outreach.EventIn.model_validate(request.data)
        facility = get_facility(spec.facility)
        outreach.require(
            request.user, facility, MANAGE, "You can't create events at this facility"
        )
        errors = outreach.Errors()
        outreach.check_event(spec, facility, errors)
        errors.raise_if_any()
        event = outreach.write_event(spec, facility, request.user)
        return Response(
            outreach.event_detail(event, request.user), status=status.HTTP_201_CREATED
        )


class OutreachEventDetailView(EmergencyAPIView):
    ACTIONS = {"submit", "approve", "reject", "new-version", "close", "cancel"}

    def get(self, request, event_id):
        event = get_event(event_id, request.user)
        return Response(outreach.event_detail(event, request.user))

    def put(self, request, event_id):
        event = get_event(event_id, request.user)
        outreach.require(
            request.user,
            event.facility,
            MANAGE,
            "You can't edit events at this facility",
        )
        if event.status not in outreach.EDITABLE:
            raise ValidationError(
                "Only a draft or rejected event can be changed; make a new version first"
            )
        spec = outreach.EventIn.model_validate(
            {**request.data, "facility": str(event.facility.external_id)}
        )
        errors = outreach.Errors()
        outreach.check_event(spec, event.facility, errors, event=event)
        errors.raise_if_any()
        event = outreach.write_event(spec, event.facility, request.user, event)
        return Response(outreach.event_detail(event, request.user))

    def post(self, request, event_id):
        """A status change: {"action": submit|approve|reject|new-version|close|cancel, "remarks"}."""
        event = get_event(event_id, request.user)
        action = request.data.get("action")
        if action not in self.ACTIONS:
            raise ValidationError("Unknown action")
        user, facility = request.user, event.facility
        if action in {"approve", "reject"}:
            outreach.require(
                user, facility, APPROVE, "Only a facility admin can approve events"
            )
            decision = outreach.APPROVED if action == "approve" else outreach.REJECTED
            event = outreach.decide_event(
                event, user, decision, request.data.get("remarks")
            )
        else:
            outreach.require(
                user, facility, MANAGE, "You can't change events at this facility"
            )
            change = {
                "submit": outreach.submit_event,
                "new-version": outreach.new_version,
                "close": outreach.close_event,
                "cancel": outreach.cancel_event,
            }[action]
            event = change(event, user)
        return Response(outreach.event_detail(event, user))


class OutreachEventParticipantsView(EmergencyAPIView):
    def get(self, request, event_id):
        event = get_event(event_id, request.user)
        participants = (
            CampParticipant.objects.filter(event=event, deleted=False)
            .select_related("patient", "captured_by")
            .prefetch_related("tests")
            .order_by("-captured_at")
        )
        return Response(
            {"results": [outreach.participant_row(p) for p in participants]}
        )


class OutreachFormTemplateView(EmergencyAPIView):
    def get(self, request):
        facility = get_facility(request.query_params.get("facility"))
        outreach.require(
            request.user, facility, MANAGE, "You can't see this facility's forms"
        )
        templates = OutreachFormTemplate.objects.filter(
            facility=facility, deleted=False
        ).order_by("name")
        return Response({"results": [outreach.template_row(t) for t in templates]})

    def post(self, request):
        facility = get_facility(request.data.get("facility"))
        outreach.require(
            request.user, facility, MANAGE, "You can't save forms at this facility"
        )
        fields = request.data.get("fields")
        if not isinstance(fields, list):
            raise ValidationError("fields must be a list")
        template = outreach.save_template(
            facility, request.user, request.data.get("name"), fields
        )
        return Response(outreach.template_row(template), status=status.HTTP_201_CREATED)


class OutreachFormTemplateDetailView(EmergencyAPIView):
    def delete(self, request, template_id):
        template = get_object_or_404(
            OutreachFormTemplate, external_id=template_id, deleted=False
        )
        outreach.require(
            request.user,
            template.facility,
            MANAGE,
            "You can't delete forms at this facility",
        )
        template.deleted = True
        template.updated_by = request.user
        template.save(update_fields=["deleted", "updated_by", "modified_date"])
        return Response(status=status.HTTP_204_NO_CONTENT)


# --- Camp mode ---------------------------------------------------------------


def camp_event_row(event):
    tests = [
        {
            "id": str(t.external_id),
            "name": t.name,
            "location": t.location,
            "fee_type": t.fee_type,
            "rate": outreach.money(t.rate),
            "quantity_offered": t.quantity_offered,
            "free_used": outreach.free_used(event, t) if t.fee_type == "free" else None,
        }
        for t in event.tests.all()
    ]
    return {
        **outreach.event_summary(
            event, event.participants.filter(deleted=False).count()
        ),
        "theme": event.theme,
        "address": event.address,
        "pincode": event.pincode,
        "tests": tests,
        "form_fields": [outreach.form_field_row(f) for f in event.form_fields.all()],
    }


def get_session(session_id, user):
    session = get_object_or_404(
        CampSession.objects.select_related("event__facility"), external_id=session_id
    )
    return outreach.open_session(session, user)


class CampEventView(EmergencyAPIView):
    """Approved camps the user can capture at (the first step of camp mode)."""

    def get(self, request):
        events = outreach.camp_events(request.user)
        counts = outreach.participant_counts(events)
        return Response(
            {
                "results": [
                    outreach.event_summary(e, counts.get(e.id, 0)) for e in events
                ]
            }
        )


class CampSessionView(EmergencyAPIView):
    def post(self, request):
        event = get_object_or_404(
            OutreachEvent.objects.select_related("facility"),
            external_id=request.data.get("event"),
            deleted=False,
        )
        session = outreach.start_session(
            event, request.user, request.data.get("device_label")
        )
        return Response(
            {
                "id": str(session.external_id),
                "started_at": session.started_at,
                "event": camp_event_row(event),
            },
            status=status.HTTP_201_CREATED,
        )


class CampSessionDetailView(EmergencyAPIView):
    def get(self, request, session_id):
        session = get_session(session_id, request.user)
        return Response(
            {
                "id": str(session.external_id),
                "started_at": session.started_at,
                "event": camp_event_row(session.event),
            }
        )


class CampSessionEndView(EmergencyAPIView):
    def post(self, request, session_id):
        session = get_object_or_404(
            CampSession, external_id=session_id, user=request.user
        )
        if not session.ended_at:
            session.ended_at = outreach.timezone.now()
            session.save(update_fields=["ended_at"])
        return Response({"id": str(session.external_id), "ended_at": session.ended_at})


class CampParticipantView(EmergencyAPIView):
    def get(self, request, session_id):
        """People captured at the session's event, newest first."""
        session = get_session(session_id, request.user)
        participants = (
            CampParticipant.objects.filter(event=session.event, deleted=False)
            .select_related("patient", "captured_by")
            .prefetch_related("tests")
            .order_by("-captured_at")[:200]
        )
        return Response(
            {"results": [outreach.participant_row(p) for p in participants]}
        )

    def post(self, request, session_id):
        session = get_session(session_id, request.user)
        spec = outreach.ParticipantIn.model_validate(request.data)
        participant = outreach.capture_participant(session, request.user, spec)
        return Response(
            outreach.participant_row(participant), status=status.HTTP_201_CREATED
        )


class CampPatientLookupView(EmergencyAPIView):
    def get(self, request, session_id):
        get_session(session_id, request.user)
        return Response(
            {"results": outreach.lookup_patients(request.query_params.get("q"))}
        )
