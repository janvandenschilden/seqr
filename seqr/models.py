from abc import abstractmethod
import uuid
import json
import random

from django.contrib.auth.models import User, Group
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import PermissionDenied
from django.db import models
from django.db.models import base, options, ForeignKey, JSONField
from django.utils import timezone
from django.utils.text import slugify as __slugify

from guardian.shortcuts import assign_perm

from seqr.utils.logging_utils import log_model_update, log_model_bulk_update, SeqrLogger
from seqr.utils.xpos_utils import get_chrom_pos
from reference_data.models import GENOME_VERSION_GRCh37, GENOME_VERSION_CHOICES
from settings import MME_DEFAULT_CONTACT_NAME, MME_DEFAULT_CONTACT_HREF, MME_DEFAULT_CONTACT_INSTITUTION

logger = SeqrLogger(__name__)

#  Allow adding the custom json_fields and internal_json_fields to the model Meta
# (from https://stackoverflow.com/questions/1088431/adding-attributes-into-django-models-meta-class)
options.DEFAULT_NAMES = options.DEFAULT_NAMES + ('json_fields', 'internal_json_fields', 'audit_fields')

CAN_VIEW = 'can_view'
CAN_EDIT = 'can_edit'


def _slugify(text):
    # using _ instead of - makes ids easier to select, and use without quotes in a wider set of contexts
    return __slugify(text).replace('-', '_')


def _get_audit_fields(audit_field):
    return {
        '{}_last_modified_date'.format(audit_field): models.DateTimeField(null=True, blank=True, db_index=True),
        '{}_last_modified_by'.format(audit_field): models.ForeignKey(User, null=True, blank=True, related_name='+', on_delete=models.SET_NULL)
    }


def get_audit_field_names(audit_field):
    return list(_get_audit_fields(audit_field).keys())


class CustomModelBase(base.ModelBase):
    def __new__(cls, name, bases, attrs, **kwargs):
        audit_fields = getattr(attrs.get('Meta'), 'audit_fields', None)
        if audit_fields:
            for audit_field in audit_fields:
                attrs.update(_get_audit_fields(audit_field))
        return super().__new__(cls, name, bases, attrs, **kwargs)


class ModelWithGUID(models.Model, metaclass=CustomModelBase):
    MAX_GUID_SIZE = 30

    guid = models.CharField(max_length=MAX_GUID_SIZE, db_index=True, unique=True)

    created_date = models.DateTimeField(default=timezone.now, db_index=True)
    created_by = models.ForeignKey(User, null=True, blank=True, related_name='+', on_delete=models.SET_NULL)

    # used for optimistic concurrent write protection (to detect concurrent changes)
    last_modified_date = models.DateTimeField(null=True, blank=True,  db_index=True)

    class Meta:
        abstract = True

        json_fields = []
        internal_json_fields = []
        audit_fields = set()

    @abstractmethod
    def _compute_guid(self):
        """Returns a human-readable label (aka. slug) for this object with only alphanumeric
        chars, '-' and '_'. This label doesn't need to be globally unique by itself, but should not
        be null or blank, and should be globally unique when paired with this object's created-time
        in seconds.
        """

    def __unicode__(self):
        return self.guid

    def __str__(self):
        """Magix function for str() and %s."""
        return self.__unicode__()

    def json(self):
        """Utility method that returns a json {field-name: value-as-string} mapping for all fields."""
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}

    def save(self, *args, **kwargs):
        """Create a GUID at object creation time."""

        being_created = not self.pk
        current_time = timezone.now()

        # allows for overriding last_modified_date during save, but this should only be used for migrations
        self.last_modified_date = kwargs.pop('last_modified_date', current_time)

        if not being_created:
            super(ModelWithGUID, self).save(*args, **kwargs)
        else:
            # do an initial save to generate the self.pk id which is then used when computing self._compute_guid()
            # Temporarily set guid to a randint to avoid a brief window when guid="". Otherwise guid uniqueness errors
            # can occur if 2 objects are being created simultaneously and both attempt to save without setting guid.
            temp_guid = str(random.randint(10**10, 10**11)) # nosec
            self.guid = kwargs.pop('guid', temp_guid)
            # allows for overriding created_date during save, but this should only be used for migrations
            self.created_date = kwargs.pop('created_date', current_time)
            super(ModelWithGUID, self).save(*args, **kwargs)

            self.guid = self._compute_guid()[:ModelWithGUID.MAX_GUID_SIZE]
            super(ModelWithGUID, self).save()

    def delete_model(self, user, user_can_delete=False):
        """Helper delete method that logs the deletion"""
        if not (user_can_delete or self.created_by == user):
            raise PermissionDenied('User does not have permission to delete this {}'.format(type(self).__name__))
        self.delete()
        log_model_update(logger, self, user, 'delete')

    @classmethod
    def bulk_create(cls, user, new_models):
        """Helper bulk create method that logs the creation"""
        for model in new_models:
            model.created_by = user
        models = cls.objects.bulk_create(new_models)
        log_model_bulk_update(logger, models, user, 'create')
        return models

    @classmethod
    def bulk_update(cls, user, update_json, queryset=None, **filter_kwargs):
        """Helper bulk update method that logs the update"""
        if queryset is None:
            queryset = cls.objects.filter(**filter_kwargs)

        entity_ids = log_model_bulk_update(logger, queryset, user, 'update', update_fields=update_json.keys())
        queryset.update(**update_json)
        return entity_ids

    @classmethod
    def bulk_delete(cls, user, queryset=None, **filter_kwargs):
        """Helper bulk delete method that logs the deletion"""
        if queryset is None:
            queryset = cls.objects.filter(**filter_kwargs)
        log_model_bulk_update(logger, queryset, user, 'delete')
        return queryset.delete()


class WarningMessage(models.Model):
    message =  models.TextField()
    header = models.TextField(null=True, blank=True)

    def json(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}


class UserPolicy(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    privacy_version = models.FloatField(null=True)
    tos_version = models.FloatField(null=True)


class Project(ModelWithGUID):
    name = models.TextField()  # human-readable project name
    description = models.TextField(null=True, blank=True)

    # user groups that allow Project permissions to be extended to other objects as long as
    # the user remains is in one of these groups.
    can_edit_group = models.ForeignKey(Group, related_name='+', on_delete=models.PROTECT)
    can_view_group = models.ForeignKey(Group, related_name='+', on_delete=models.PROTECT)

    genome_version = models.CharField(max_length=5, choices=GENOME_VERSION_CHOICES, default=GENOME_VERSION_GRCh37)

    is_mme_enabled = models.BooleanField(default=True)
    mme_primary_data_owner = models.TextField(null=True, blank=True, default=MME_DEFAULT_CONTACT_NAME)
    mme_contact_url = models.TextField(null=True, blank=True, default=MME_DEFAULT_CONTACT_HREF)
    mme_contact_institution = models.TextField(null=True, blank=True, default=MME_DEFAULT_CONTACT_INSTITUTION)

    has_case_review = models.BooleanField(default=False)
    enable_hgmd = models.BooleanField(default=False)
    all_user_demo = models.BooleanField(default=False)
    is_demo = models.BooleanField(default=False)

    last_accessed_date = models.DateTimeField(null=True, blank=True, db_index=True)

    workspace_namespace = models.TextField(null = True, blank = True)
    workspace_name = models.TextField(null = True, blank = True)

    def __unicode__(self):
        return self.name.strip()

    def _compute_guid(self):
        return 'R%04d_%s' % (self.id, _slugify(str(self)))

    def save(self, *args, **kwargs):
        """Override the save method and create user permissions groups + add the created_by user.

        This could be done with signals, but seems cleaner to do it this way.
        """
        being_created = not self.pk

        if being_created:
            # create user groups
            self.can_edit_group = Group.objects.create(name="%s_%s_%s" % (_slugify(self.name.strip())[:30], 'can_edit', uuid.uuid4()))
            self.can_view_group = Group.objects.create(name="%s_%s_%s" % (_slugify(self.name.strip())[:30], 'can_view', uuid.uuid4()))

        super(Project, self).save(*args, **kwargs)

        if being_created:
            assign_perm(user_or_group=self.can_edit_group, perm=CAN_EDIT, obj=self)
            assign_perm(user_or_group=self.can_edit_group, perm=CAN_VIEW, obj=self)

            assign_perm(user_or_group=self.can_view_group, perm=CAN_VIEW, obj=self)

            # add the user that created this Project to all permissions groups
            user = self.created_by
            user.groups.add(self.can_edit_group, self.can_view_group)

    def delete(self, *args, **kwargs):
        """Override the delete method to also delete the project-specific user groups"""

        super(Project, self).delete(*args, **kwargs)

        self.can_edit_group.delete()
        self.can_view_group.delete()

    def get_collaborators(self, permissions=None):
        if not permissions:
            permissions = {CAN_VIEW, CAN_EDIT}

        collabs = set()
        if CAN_VIEW in permissions:
            collabs.update(self.can_view_group.user_set.all())
        if CAN_EDIT in permissions:
            collabs.update(self.can_edit_group.user_set.all())

        return collabs

    class Meta:
        permissions = (
            (CAN_VIEW, CAN_VIEW),
            (CAN_EDIT, CAN_EDIT),
        )

        json_fields = [
            'name', 'description', 'created_date', 'last_modified_date', 'genome_version', 'mme_contact_institution',
            'last_accessed_date', 'is_mme_enabled', 'mme_primary_data_owner', 'mme_contact_url', 'guid',
            'workspace_namespace', 'workspace_name', 'has_case_review', 'enable_hgmd', 'is_demo', 'all_user_demo',
        ]


class ProjectCategory(ModelWithGUID):
    projects = models.ManyToManyField('Project')
    name = models.TextField(db_index=True)  # human-readable category name
    # color = models.CharField(max_length=20, default="#1f78b4")

    def __unicode__(self):
        return self.name.strip()

    def _compute_guid(self):
        return 'PC%06d_%s' % (self.id, _slugify(str(self)))


class Family(ModelWithGUID):
    ANALYSIS_STATUS_ANALYSIS_IN_PROGRESS='I'
    ANALYSIS_STATUS_WAITING_FOR_DATA='Q'
    ANALYSIS_STATUS_CHOICES = (
        ('S', 'Solved'),
        ('S_kgfp', 'Solved - known gene for phenotype'),
        ('S_kgdp', 'Solved - gene linked to different phenotype'),
        ('S_ng', 'Solved - novel gene'),
        ('ES', 'External solve'),
        ('Sc_kgfp', 'Strong candidate - known gene for phenotype'),
        ('Sc_kgdp', 'Strong candidate - gene linked to different phenotype'),
        ('Sc_ng', 'Strong candidate - novel gene'),
        ('Rcpc', 'Reviewed, currently pursuing candidates'),
        ('Rncc', 'Reviewed, no clear candidate'),
        ('C', 'Closed, no longer under analysis'),
        ('I', 'Analysis in Progress'),
        ('Q', 'Waiting for data'),
        ('N', 'No data expected'),
    )

    SUCCESS_STORY_TYPE_CHOICES = (
        ('N', 'Novel Discovery'),
        ('A', 'Altered Clinical Outcome'),
        ('C', 'Collaboration'),
        ('T', 'Technical Win'),
        ('D', 'Data Sharing'),
        ('O', 'Other'),
    )

    project = models.ForeignKey('Project', on_delete=models.PROTECT)

    # WARNING: family_id is unique within a project, but not necessarily unique globally.
    family_id = models.CharField(db_index=True, max_length=100)
    display_name = models.CharField(db_index=True, max_length=100, null=True, blank=True)  # human-readable name

    description = models.TextField(null=True, blank=True)

    pedigree_image = models.ImageField(null=True, blank=True, upload_to='pedigree_images')
    pedigree_dataset = JSONField(null=True, blank=True)

    assigned_analyst = models.ForeignKey(User, null=True, on_delete=models.SET_NULL,
                                    related_name='assigned_families')  # type: ForeignKey

    success_story_types = ArrayField(models.CharField(
        max_length=1,
        choices=SUCCESS_STORY_TYPE_CHOICES,
        null=True,
        blank=True
    ), default=list)
    success_story = models.TextField(null=True, blank=True)

    coded_phenotype = models.TextField(null=True, blank=True)
    post_discovery_omim_number = models.TextField(null=True, blank=True)
    pubmed_ids = ArrayField(models.TextField(), default=list)

    analysis_status = models.CharField(
        max_length=10,
        choices=[(s[0], s[1][0]) for s in ANALYSIS_STATUS_CHOICES],
        default="Q"
    )

    case_review_notes = models.TextField(null=True, blank=True)
    case_review_summary = models.TextField(null=True, blank=True)

    def __unicode__(self):
        return self.family_id.strip()

    def _compute_guid(self):
        return 'F%06d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        unique_together = ('project', 'family_id')

        json_fields = [
            'guid', 'family_id', 'display_name', 'description', 'analysis_status', 'pedigree_image', 'created_date',
            'post_discovery_omim_number', 'assigned_analyst', 'pedigree_dataset', 'coded_phenotype',
        ]
        internal_json_fields = [
            'success_story_types', 'success_story', 'pubmed_ids',
        ]
        audit_fields = {'analysis_status'}


class FamilyAnalysedBy(ModelWithGUID):
    DATA_TYPE_CHOICES = (
        ('SNP', 'WES/WGS'),
        ('SV', 'gCNV/SV'),
        ('RNA', 'RNAseq'),
        ('MT', 'Mitochondrial'),
        ('STR', 'STR'),
    )

    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    data_type = models.CharField(max_length=3, choices=DATA_TYPE_CHOICES)

    def __unicode__(self):
        return '{}_{}_{}'.format(self.family.guid, self.created_by, self.data_type)

    def _compute_guid(self):
        return 'FAB%06d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        json_fields = ['last_modified_date', 'created_by', 'data_type']


class FamilyNote(ModelWithGUID):
    NOTE_TYPE_CHOICES = (
        ('M', 'mme'),
        ('C', 'case'),
        ('A', 'analysis'),
    )

    family = models.ForeignKey(Family, on_delete=models.CASCADE)
    note = models.TextField()
    note_type = models.CharField(max_length=1, choices=NOTE_TYPE_CHOICES,)

    def __unicode__(self):
        return '{}_{}_{}'.format(self.family.family_id, self.note_type, self.note)[:20]

    def _compute_guid(self):
        return 'FAN{:06d}_{}'.format(self.id, _slugify(str(self)))

    class Meta:
        json_fields = ['guid', 'note', 'note_type', 'last_modified_date', 'created_by']


class YearField(models.PositiveSmallIntegerField):
    YEAR_CHOICES = [(y, y) for y in range(1900, 2030)] + [(0, 'Unknown')]

    def __init__(self, *args, **kwargs):
        super(YearField, self).__init__(*args, choices=YearField.YEAR_CHOICES, null=True)


class Individual(ModelWithGUID):
    SEX_MALE = 'M'
    SEX_FEMALE = 'F'
    SEX_UNKNOWN = 'U'
    SEX_CHOICES = (
        (SEX_MALE, 'Male'),
        ('F', 'Female'),
        ('U', 'Unknown'),
    )

    AFFECTED_STATUS_AFFECTED = 'A'
    AFFECTED_STATUS_UNAFFECTED = 'N'
    AFFECTED_STATUS_UNKNOWN = 'U'
    AFFECTED_STATUS_CHOICES = (
        (AFFECTED_STATUS_AFFECTED, 'Affected'),
        (AFFECTED_STATUS_UNAFFECTED, 'Unaffected'),
        (AFFECTED_STATUS_UNKNOWN, 'Unknown'),
    )

    CASE_REVIEW_STATUS_IN_REVIEW = "I"
    CASE_REVIEW_STATUS_CHOICES = (
        ('I', 'In Review'),
        ('U', 'Uncertain'),
        ('A', 'Accepted'),
        ('R', 'Not Accepted'),
        ('Q', 'More Info Needed'),
        ('P', 'Pending Results and Records'),
        ('N', 'NMI Review'),
        ('W', 'Waitlist'),
        ('L', 'Lost To Follow-Up'),
        ('V', 'Inactive'),
    )

    ONSET_AGE_CHOICES = [
        ('G', 'Congenital onset'),
        ('E', 'Embryonal onset'),
        ('F', 'Fetal onset'),
        ('N', 'Neonatal onset'),
        ('I', 'Infantile onset'),
        ('C', 'Childhood onset'),
        ('J', 'Juvenile onset'),
        ('A', 'Adult onset'),
        ('Y', 'Young adult onset'),
        ('M', 'Middle age onset'),
        ('L', 'Late onset'),
    ]

    INHERITANCE_CHOICES = [
        ('S', 'Sporadic'),
        ('D', 'Autosomal dominant inheritance'),
        ('L', 'Sex-limited autosomal dominant'),
        ('A', 'Male-limited autosomal dominant'),
        ('C', 'Autosomal dominant contiguous gene syndrome'),
        ('R', 'Autosomal recessive inheritance'),
        ('G', 'Gonosomal inheritance'),
        ('X', 'X-linked inheritance'),
        ('Z', 'X-linked recessive inheritance'),
        ('Y', 'Y-linked inheritance'),
        ('W', 'X-linked dominant inheritance'),
        ('F', 'Multifactorial inheritance'),
        ('M', 'Mitochondrial inheritance'),
    ]

    FEMALE_RELATIONSHIP_CHOICES = {
        'M': 'Mother',
        'G': 'Maternal Grandmother',
        'X': 'Paternal Grandmother',
        'A': 'Maternal Aunt',
        'E': 'Paternal Aunt',
        'N': 'Niece',
    }

    MALE_RELATIONSHIP_CHOICES = {
        'F': 'Father',
        'W': 'Maternal Grandfather',
        'Y': 'Paternal Grandfather',
        'L': 'Maternal Uncle',
        'D': 'Paternal Uncle',
        'P': 'Nephew',
    }

    RELATIONSHIP_CHOICES = list(FEMALE_RELATIONSHIP_CHOICES.items()) + list(MALE_RELATIONSHIP_CHOICES.items()) + [
        ('S', 'Self'),
        ('B', 'Sibling'),
        ('C', 'Child'),
        ('H', 'Maternal Half Sibling'),
        ('J', 'Paternal Half Sibling'),
        ('Z', 'Maternal 1st Cousin'),
        ('K', 'Paternal 1st Cousin'),
        ('O', 'Other'),
        ('U', 'Unknown'),
    ]

    SEX_LOOKUP = dict(SEX_CHOICES)
    AFFECTED_STATUS_LOOKUP = dict(AFFECTED_STATUS_CHOICES)
    CASE_REVIEW_STATUS_LOOKUP = dict(CASE_REVIEW_STATUS_CHOICES)
    CASE_REVIEW_STATUS_REVERSE_LOOKUP = {name.lower(): key for key, name in CASE_REVIEW_STATUS_CHOICES}
    ONSET_AGE_LOOKUP = dict(ONSET_AGE_CHOICES)
    ONSET_AGE_REVERSE_LOOKUP = {name: key for key, name in ONSET_AGE_CHOICES}
    INHERITANCE_LOOKUP = dict(INHERITANCE_CHOICES)
    INHERITANCE_REVERSE_LOOKUP = {name: key for key, name in INHERITANCE_CHOICES}
    RELATIONSHIP_LOOKUP = dict(RELATIONSHIP_CHOICES)

    family = models.ForeignKey(Family, on_delete=models.PROTECT)

    # WARNING: individual_id is unique within a family, but not necessarily unique globally
    individual_id = models.TextField(db_index=True)

    mother = models.ForeignKey('seqr.Individual', null=True, blank=True, on_delete=models.SET_NULL, related_name='maternal_children')
    father = models.ForeignKey('seqr.Individual', null=True, blank=True, on_delete=models.SET_NULL, related_name='paternal_children')

    sex = models.CharField(max_length=1, choices=SEX_CHOICES, default='U')
    affected = models.CharField(max_length=1, choices=AFFECTED_STATUS_CHOICES, default=AFFECTED_STATUS_UNKNOWN)

    # TODO once sample and individual ids are fully decoupled no reason to maintain this field
    display_name = models.TextField(default="", blank=True)

    notes = models.TextField(blank=True, null=True)

    case_review_status = models.CharField(max_length=2, choices=CASE_REVIEW_STATUS_CHOICES, default=CASE_REVIEW_STATUS_IN_REVIEW)
    case_review_discussion = models.TextField(null=True, blank=True)

    proband_relationship = models.CharField(max_length=1, choices=RELATIONSHIP_CHOICES, null=True)

    birth_year = YearField()
    death_year = YearField()
    onset_age = models.CharField(max_length=1, choices=ONSET_AGE_CHOICES, null=True)

    maternal_ethnicity = ArrayField(models.CharField(max_length=40), null=True)
    paternal_ethnicity = ArrayField(models.CharField(max_length=40), null=True)
    consanguinity = models.BooleanField(null=True)
    affected_relatives = models.BooleanField(null=True)
    expected_inheritance = ArrayField(models.CharField(max_length=1, choices=INHERITANCE_CHOICES), null=True)

    # features are objects with an id field for HPO id and optional notes and qualifiers fields
    features = JSONField(null=True)
    absent_features = JSONField(null=True)
    # nonstandard_features are objects with an id field for a free text label and optional
    # notes, qualifiers, and categories fields
    nonstandard_features = JSONField(null=True)
    absent_nonstandard_features = JSONField(null=True)

    # Disorders are a list of MIM IDs
    disorders = ArrayField(models.CharField(max_length=10), null=True)

    # genes are objects with required key gene (may be blank) and optional key comments
    candidate_genes = JSONField(null=True)
    rejected_genes = JSONField(null=True)

    ar_fertility_meds = models.BooleanField(null=True)
    ar_iui = models.BooleanField(null=True)
    ar_ivf = models.BooleanField(null=True)
    ar_icsi = models.BooleanField(null=True)
    ar_surrogacy = models.BooleanField(null=True)
    ar_donoregg = models.BooleanField(null=True)
    ar_donorsperm = models.BooleanField(null=True)

    filter_flags = JSONField(null=True)
    pop_platform_filters = JSONField(null=True)
    population = models.CharField(max_length=5, null=True)
    sv_flags = JSONField(null=True)

    def __unicode__(self):
        return self.individual_id.strip()

    def _compute_guid(self):
        return 'I%07d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        unique_together = ('family', 'individual_id')

        json_fields = [
            'guid', 'individual_id', 'father', 'mother', 'sex', 'affected', 'display_name', 'notes',
            'created_date', 'last_modified_date', 'filter_flags', 'pop_platform_filters', 'population', 'sv_flags',
            'birth_year', 'death_year', 'onset_age', 'maternal_ethnicity', 'paternal_ethnicity', 'consanguinity',
            'affected_relatives', 'expected_inheritance', 'disorders', 'candidate_genes', 'rejected_genes',
            'ar_iui', 'ar_ivf', 'ar_icsi', 'ar_surrogacy', 'ar_donoregg', 'ar_donorsperm', 'ar_fertility_meds',
        ]
        internal_json_fields = [
            'proband_relationship'
        ]
        audit_fields = {'case_review_status'}


class Sample(ModelWithGUID):
    """This model represents a single data type (eg. Variant Calls, or SV Calls) that's generated from a single
    biological sample (eg. WES, WGS).

    It stores metadata on both the dataset (fields: dataset_type, loaded_date, etc.) and the underlying sample
    (fields: sample_type, sample_id etc.)
    """

    SAMPLE_TYPE_WES = 'WES'
    SAMPLE_TYPE_WGS = 'WGS'
    SAMPLE_TYPE_RNA = 'RNA'
    SAMPLE_TYPE_CHOICES = (
        (SAMPLE_TYPE_WES, 'Exome'),
        (SAMPLE_TYPE_WGS, 'Whole Genome'),
        (SAMPLE_TYPE_RNA, 'RNA'),
    )

    DATASET_TYPE_VARIANT_CALLS = 'VARIANTS'
    DATASET_TYPE_SV_CALLS = 'SV'
    DATASET_TYPE_CHOICES = (
        (DATASET_TYPE_VARIANT_CALLS, 'Variant Calls'),
        (DATASET_TYPE_SV_CALLS, 'SV Calls'),
    )
    DATASET_TYPE_LOOKUP = dict(DATASET_TYPE_CHOICES)

    TISSUE_TYPE_CHOICES = (
        ('WB', 'Whole Blood'),
        ('F', 'Fibroblast'),
        ('M', 'Muscle'),
        ('L', 'Lymphocyte'),
    )

    individual = models.ForeignKey('Individual', on_delete=models.PROTECT)

    sample_type = models.CharField(max_length=10, choices=SAMPLE_TYPE_CHOICES)
    dataset_type = models.CharField(max_length=10, choices=DATASET_TYPE_CHOICES)

    tissue_type = models.CharField(max_length=2, choices=TISSUE_TYPE_CHOICES, null=True, blank=True)

    # The sample's id in the underlying dataset (eg. the VCF Id for variant callsets).
    sample_id = models.TextField(db_index=True)

    elasticsearch_index = models.TextField(db_index=True, null=True)
    data_source = models.TextField(null=True)

    # sample status
    is_active = models.BooleanField(default=False)
    loaded_date = models.DateTimeField()

    def __unicode__(self):
        return self.sample_id.strip()

    def _compute_guid(self):
        return 'S%010d_%s' % (self.id, _slugify(str(self)))

    class Meta:
       json_fields = [
           'guid', 'created_date', 'sample_type', 'dataset_type', 'sample_id', 'is_active', 'loaded_date',
           'elasticsearch_index',
       ]


class IgvSample(ModelWithGUID):
    """This model represents a single data type that can be displayed in IGV (eg. Read Alignments) that's generated from
    a single biological sample (eg. WES, WGS, RNA, Array).
    """
    SAMPLE_TYPE_ALIGNMENT = 'alignment'
    SAMPLE_TYPE_COVERAGE = 'wig'
    SAMPLE_TYPE_JUNCTION = 'spliceJunctions'
    SAMPLE_TYPE_GCNV = 'gcnv'
    SAMPLE_TYPE_CHOICES = (
        (SAMPLE_TYPE_ALIGNMENT, 'Bam/Cram'),
        (SAMPLE_TYPE_COVERAGE, 'RNAseq Coverage'),
        (SAMPLE_TYPE_JUNCTION, 'RNAseq Junction'),
        (SAMPLE_TYPE_GCNV, 'gCNV'),
    )

    individual = models.ForeignKey('Individual', on_delete=models.PROTECT)
    sample_type = models.CharField(max_length=15, choices=SAMPLE_TYPE_CHOICES)
    file_path = models.TextField()
    sample_id = models.TextField(null=True)

    def __unicode__(self):
        return self.file_path.split('/')[-1].split('.')[0].strip()

    def _compute_guid(self):
        return 'S%010d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        unique_together = ('individual', 'sample_type')

        json_fields = ['guid', 'file_path', 'sample_type', 'sample_id']


class SavedVariant(ModelWithGUID):
    family = models.ForeignKey('Family', on_delete=models.CASCADE)

    xpos = models.BigIntegerField()
    xpos_end = models.BigIntegerField(null=True)
    ref = models.TextField(null=True)
    alt = models.TextField(null=True)
    variant_id = models.TextField(db_index=True)

    selected_main_transcript_id = models.CharField(max_length=20, null=True)
    saved_variant_json = JSONField(default=dict)

    acmg_classification = JSONField(null=True) # ACMG based classification

    def __unicode__(self):
        chrom, pos = get_chrom_pos(self.xpos)
        return "%s:%s-%s" % (chrom, pos, self.family.guid)

    def _compute_guid(self):
        return 'SV%07d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        unique_together = ('xpos', 'xpos_end', 'variant_id', 'family')

        json_fields = ['guid', 'xpos', 'ref', 'alt', 'variant_id', 'selected_main_transcript_id', 'acmg_classification']


class VariantTagType(ModelWithGUID):
    """
    Previous color choices:
        '#1f78b4',
        '#a6cee3',
        '#b2df8a',
        '#33a02c',
        '#fdbf6f',
        '#ff7f00',
        '#ff0000',
        '#cab2d6',
        '#6a3d9a',
        '#8F754F',
        '#383838',
    """
    project = models.ForeignKey('Project', null=True, blank=True, on_delete=models.CASCADE)

    name = models.TextField()
    category = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    color = models.CharField(max_length=20, default="#1f78b4")
    order = models.FloatField(null=True)
    metadata_title = models.CharField(max_length=20, null=True, blank=True)

    def __unicode__(self):
        return self.name.strip()

    def _compute_guid(self):
        return 'VTT%05d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        unique_together = ('project', 'name', 'color')

        json_fields = ['guid', 'name', 'category', 'description', 'color', 'order', 'metadata_title']


class VariantTag(ModelWithGUID):
    saved_variants = models.ManyToManyField('SavedVariant')
    variant_tag_type = models.ForeignKey('VariantTagType', on_delete=models.CASCADE)
    metadata = models.TextField(null=True)

    # context in which a variant tag was saved
    search_hash = models.CharField(max_length=50, null=True)

    def __unicode__(self):
        saved_variants_ids = "".join(str(saved_variant) for saved_variant in self.saved_variants.all())
        return "%s:%s" % (saved_variants_ids, self.variant_tag_type.name)

    def _compute_guid(self):
        return 'VT%07d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        json_fields = ['guid', 'search_hash', 'metadata', 'last_modified_date', 'created_by']


class VariantNote(ModelWithGUID):
    saved_variants = models.ManyToManyField('SavedVariant')
    note = models.TextField()
    submit_to_clinvar = models.BooleanField(default=False)

    # these are for context
    search_hash = models.CharField(max_length=50, null=True)

    def __unicode__(self):
        saved_variants_ids = "".join(str(saved_variant) for saved_variant in self.saved_variants.all())
        return "%s:%s" % (saved_variants_ids, (self.note or "")[:20])

    def _compute_guid(self):
        return 'VN%07d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        json_fields = ['guid', 'note', 'submit_to_clinvar', 'last_modified_date', 'created_by']


class VariantFunctionalData(ModelWithGUID):
    FUNCTIONAL_DATA_CHOICES = (
        ('Functional Data', (
            ('Biochemical Function', json.dumps({
                'description': 'Gene product performs a biochemical function shared with other known genes in the disease of interest, or consistent with the phenotype.',
                'color': '#311B92',
            })),
            ('Protein Interaction', json.dumps({
                'description': 'Gene product interacts with proteins previously implicated (genetically or biochemically) in the disease of interest.',
                'color': '#4A148C',
            })),
            ('Expression', json.dumps({
                'description': 'Gene is expressed in tissues relevant to the disease of interest and/or is altered in expression in patients who have the disease.',
                'color': '#7C4DFF',
            })),
            ('Patient Cells', json.dumps({
                'description': 'Gene and/or gene product function is demonstrably altered in patients carrying candidate mutations.',
                'color': '#B388FF',
            })),
            ('Non-patient cells', json.dumps({
                'description': 'Gene and/or gene product function is demonstrably altered in human cell culture models carrying candidate mutations.',
                'color': '#9575CD',
            })),
            ('Animal Model', json.dumps({
                'description': 'Non-human animal models with a similarly disrupted copy of the affected gene show a phenotype consistent with human disease state.',
                'color': '#AA00FF',
            })),
            ('Non-human cell culture model', json.dumps({
                'description': 'Non-human cell-culture models with a similarly disrupted copy of the affected gene show a phenotype consistent with human disease state.',
                'color': '#BA68C8',
            })),
            ('Rescue', json.dumps({
                'description': 'The cellular phenotype in patient-derived cells or engineered equivalents can be rescued by addition of the wild-type gene product.',
                'color': '#663399',
            })),
        )),
        ('Functional Scores', (
            ('Genome-wide Linkage', json.dumps({
                'metadata_title': 'LOD Score',
                'description': 'Max LOD score used in analysis to restrict where you looked for causal variants; provide best score available, whether it be a cumulative LOD score across multiple families or just the best family\'s LOD score.',
                'color': '#880E4F',
            })),
            ('Bonferroni corrected p-value', json.dumps({
                'metadata_title': 'P-value',
                'description': 'Bonferroni-corrected p-value for gene if association testing/burden testing/etc was used to identify the gene.',
                'color': '#E91E63',
            })),
            ('Kindreds w/ Overlapping SV & Similar Phenotype', json.dumps({
                'metadata_title': '#',
                'description': 'Number of kindreds (1+) previously reported/in databases as having structural variant overlapping the gene and a similar phenotype.',
                'color': '#FF5252',
            })),
        )),
        ('Additional Kindreds (Literature, MME)', (
             ('Additional Unrelated Kindreds w/ Causal Variants in Gene', json.dumps({
                'metadata_title': '# additional families',
                'description': 'Number of additional kindreds with causal variants in this gene (Any other kindreds from collaborators, MME, literature etc). Do not count your family in this total.',
                'color': '#D84315',
             })),
         )),
    )

    FUNCTIONAL_DATA_TAG_TYPES = [{
        'category': category,
        'name': name,
        'metadataTitle': json.loads(tag_json).get('metadata_title', 'Notes'),
        'color': json.loads(tag_json)['color'],
        'description': json.loads(tag_json).get('description'),
    } for category, tags in FUNCTIONAL_DATA_CHOICES for name, tag_json in tags]

    saved_variants = models.ManyToManyField('SavedVariant')
    functional_data_tag = models.TextField(choices=FUNCTIONAL_DATA_CHOICES)
    metadata = models.TextField(null=True)

    search_hash = models.CharField(max_length=50, null=True)

    def __unicode__(self):
        saved_variants_ids = "".join(str(saved_variant) for saved_variant in self.saved_variants.all())
        return "%s:%s" % (saved_variants_ids, self.functional_data_tag)

    def _compute_guid(self):
        return 'VFD%07d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        json_fields = ['guid', 'functional_data_tag', 'metadata', 'last_modified_date', 'created_by']


class GeneNote(ModelWithGUID):
    note = models.TextField(default="", blank=True)
    gene_id = models.CharField(max_length=20)  # ensembl ID

    def __unicode__(self):
        return "%s:%s" % (self.gene_id, (self.note or "")[:20])

    def _compute_guid(self):
        return 'GN%07d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        json_fields = ['guid', 'note', 'gene_id', 'last_modified_date', 'created_by']


class LocusList(ModelWithGUID):
    """List of gene ids or regions"""

    name = models.TextField(db_index=True)
    description = models.TextField(null=True, blank=True)

    projects = models.ManyToManyField('Project')
    is_public = models.BooleanField(default=False)

    def __unicode__(self):
        return self.name.strip()

    def _compute_guid(self):
        return 'LL%05d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        unique_together = ('name', 'description', 'is_public', 'created_by')

        json_fields = ['guid', 'created_by', 'created_date', 'last_modified_date', 'name', 'description', 'is_public']


class LocusListGene(ModelWithGUID):
    locus_list = models.ForeignKey('LocusList', on_delete=models.CASCADE)
    # TODO would be more efficient to take out this class entirely and have locus lists directly reference GeneInfo models
    gene_id = models.TextField(db_index=True)

    def __unicode__(self):
        return "%s:%s" % (self.locus_list, self.gene_id)

    def _compute_guid(self):
        return 'LLG%07d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        unique_together = ('locus_list', 'gene_id')


class LocusListInterval(ModelWithGUID):
    locus_list = models.ForeignKey('LocusList', on_delete=models.CASCADE)

    genome_version = models.CharField(max_length=5, choices=GENOME_VERSION_CHOICES, default=GENOME_VERSION_GRCh37)
    chrom = models.CharField(max_length=2)
    start = models.IntegerField()
    end = models.IntegerField()

    def __unicode__(self):
        return "%s:%s:%s-%s" % (self.locus_list, self.chrom, self.start, self.end)

    def _compute_guid(self):
        return 'LLI%07d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        unique_together = ('locus_list', 'genome_version', 'chrom', 'start', 'end')

        json_fields = ['guid', 'genome_version', 'chrom', 'start', 'end']


class AnalysisGroup(ModelWithGUID):
    name = models.TextField()
    description = models.TextField(null=True, blank=True)

    project = models.ForeignKey('Project', on_delete=models.CASCADE)
    families = models.ManyToManyField(Family)

    def __unicode__(self):
        return self.name.strip()

    def _compute_guid(self):
        return 'AG%07d_%s' % (self.id, _slugify(str(self)))

    class Meta:
        unique_together = ('project', 'name')

        json_fields = ['guid', 'name', 'description']


class VariantSearch(ModelWithGUID):
    name = models.CharField(max_length=200, null=True)
    order = models.FloatField(null=True, blank=True)
    search = JSONField()

    def __unicode__(self):
        return self.name or str(self.id)

    def _compute_guid(self):
        return 'VS%07d_%s' % (self.id, _slugify(self.name or ''))

    class Meta:
        unique_together = ('created_by', 'name')

        json_fields = ['guid', 'name', 'order', 'search', 'created_by_id']


class VariantSearchResults(ModelWithGUID):
    variant_search = models.ForeignKey('VariantSearch', on_delete=models.CASCADE)
    families = models.ManyToManyField('Family')
    search_hash = models.CharField(max_length=50, db_index=True, unique=True)

    def __unicode__(self):
        return self.search_hash

    def _compute_guid(self):
        return 'VSR%07d_%s' % (self.id, _slugify(str(self)))

class DeletableSampleMetadataModel(models.Model):

    sample = models.ForeignKey('Sample', on_delete=models.CASCADE, db_index=True)
    gene_id = models.CharField(max_length=20)  # ensembl ID

    @classmethod
    def bulk_delete(cls, user, queryset=None, **filter_kwargs):
        """Helper bulk delete method that logs the deletion"""
        if queryset is None:
            queryset = cls.objects.filter(**filter_kwargs)
        log_model_bulk_update(logger, queryset, user, 'delete')
        return queryset.delete()

    def __unicode__(self):
        return "%s:%s" % (self.sample.sample_id, self.gene_id)

    class Meta:
        abstract = True


class RnaSeqOutlier(DeletableSampleMetadataModel):
    SIGNIFICANCE_THRESHOLD = 0.05

    p_value = models.FloatField()
    p_adjust = models.FloatField()
    z_score = models.FloatField()

    class Meta:
        unique_together = ('sample', 'gene_id')

        json_fields = ['gene_id', 'p_value', 'p_adjust', 'z_score']

        indexes = [models.Index(fields=['gene_id']), models.Index(fields=['p_adjust'])]


class RnaSeqTpm(DeletableSampleMetadataModel):
    tpm = models.FloatField()

    class Meta:
        unique_together = ('sample', 'gene_id')

        json_fields = ['gene_id', 'tpm']
