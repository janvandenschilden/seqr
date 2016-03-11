from django.core.management.base import BaseCommand
from xbrowse_server import xbrowse_controls


class Command(BaseCommand):

    def add_arguments(self, parser):
        parser.add_argument('args', nargs='+')

    def handle(self, *args, **options):
        project_id = args[0]
        xbrowse_controls.load_project_datastore(project_id)