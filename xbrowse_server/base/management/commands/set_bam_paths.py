import os
import settings
from django.core.management.base import BaseCommand
from xbrowse_server.base.models import Project, Individual


class Command(BaseCommand):

    def handle(self, *args, **options):
        """Command line args:
            arg0 = project id
            arg1 = file path - where file is a tsv table with 2 columns:
                    column0: the individual ID
                    column1: bam file path relative to the base settings.READ_VIZ_BAM_PATH url or directory - so that
                        the absolute bam path can be derived by doing os.path.join(settings.READ_VIZ_BAM_PATH, column1)
        """
        project_id = args[0]
        project = Project.objects.get(project_id=project_id)

        for line in open(args[1]).readlines():
            indiv_id, bam_path = line.strip('\n').split('\t')
            indiv = Individual.objects.get(project=project, indiv_id=indiv_id)
            absolute_path = os.path.join(settings.READ_VIZ_BAM_PATH, bam_path)
            if not absolute_path.startswith('http:') and not os.path.isfile(absolute_path):
                print("ERROR: " + absolute_path + " not found")
            indiv.bam_file_path = bam_path
            indiv.save()

