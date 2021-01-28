"""APIs for management of projects related to AnVIL workspaces."""

import logging
from django.contrib.auth.decorators import login_required

from seqr.views.utils.json_utils import create_json_response
from settings import API_LOGIN_REQUIRED_URL

logger = logging.getLogger(__name__)


@login_required(login_url=API_LOGIN_REQUIRED_URL)
def anvil_workspace_page(request, namespace, name):
    """Redirect to the loading data from workspace page or redirect to the project if the project exists.

    Args:
        request (object): Django request object
        namespace: The namespace (or the billing account) of the workspace
        name: The name of the workspace. It also be used as the project name
    Return:
        Error page if the workspace doesn't exist or the user doesn't have a WRITER permission.

        Redirect to '/create_project_from_workspace/:workspace_name' if project doesn't exist

        Redirect to '/project/:projectGuid if project exists

    """
    # To be implemented
    return create_json_response({})


@login_required(login_url=API_LOGIN_REQUIRED_URL)
def create_project_from_workspace(request, namespace, name):
    """Implemented features are listed below.

    Args:
        request (object): Django request object
        namespace: The namespace (or the billing account) of the workspace
        name: The name of the workspace. It also be used as the project name
    Return:
        The projectsByGuid with the new project json

    """
    """
    Todo:
    1) Validate that the current user has logged in through google and has one of the valid can_edit levels of
     access on the specified workspace;
    2) Validate all the user input from the post body;
    3) Add the seqr service account to the corresponding AnVIL workspace, so that our team will have access to the
    project for data loading;
    4) Create a new Project in seqr. This project should NOT be added to the analyst group. The project name should
     just be the workspace name. Make sure to set workspace_namespace and workspace_name correctly;
    5) Add families/individuals based on the uploaded pedigree file;
    6) Send an email to all seqr data managers saying a new AnVIL project is ready for loading. Include the seqr
     project guid, the workspace name, and attach a txt file with a list of the individual IDs that were created.
    """
    return create_json_response({})
