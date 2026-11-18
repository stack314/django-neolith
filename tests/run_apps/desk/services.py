"""The desk app's implementation: it counts the notes in its own database."""

from neolith import CallContext
from tests.run_apps.contracts import Tally
from tests.run_apps.desk.models import Note


class DeskService:
    def count(self, request: Tally, ctx: CallContext) -> Tally:
        return Tally(notes=request.notes + Note.objects.count())
