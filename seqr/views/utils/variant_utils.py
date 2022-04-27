from collections import defaultdict
from django.db.models import prefetch_related_objects, Prefetch
import logging
import redis

from seqr.models import SavedVariant, VariantSearchResults, Family, LocusList, LocusListInterval, LocusListGene, \
    RnaSeqOutlier, RnaSeqTpm
from seqr.utils.elasticsearch.utils import get_es_variants_for_variant_ids
from seqr.utils.gene_utils import get_genes_for_variants
from seqr.views.utils.json_to_orm_utils import update_model_from_json
from seqr.views.utils.orm_to_json_utils import get_json_for_discovery_tags, get_json_for_locus_lists, \
    _get_json_for_models, get_json_for_rna_seq_outliers, get_json_for_saved_variants_with_tags
from seqr.views.utils.permissions_utils import has_case_review_permissions, user_is_analyst
from seqr.views.utils.project_context_utils import add_project_tag_types, add_families_context
from settings import REDIS_SERVICE_HOSTNAME

logger = logging.getLogger(__name__)


MAX_VARIANTS_FETCH = 1000

def update_project_saved_variant_json(project, family_id=None, user=None):
    saved_variants = SavedVariant.objects.filter(family__project=project).select_related('family')
    if family_id:
        saved_variants = saved_variants.filter(family__family_id=family_id)

    if not saved_variants:
        return []

    families = set()
    variant_ids = set()
    saved_variants_map = {}
    for v in saved_variants:
        families.add(v.family)
        variant_ids.add(v.variant_id)
        saved_variants_map[(v.variant_id, v.family.guid)] = v

    variant_ids = sorted(variant_ids)
    families = sorted(families, key=lambda f: f.guid)
    variants_json = []
    for sub_var_ids in [variant_ids[i:i+MAX_VARIANTS_FETCH] for i in range(0, len(variant_ids), MAX_VARIANTS_FETCH)]:
        variants_json += get_es_variants_for_variant_ids(families, sub_var_ids, user=user)

    updated_saved_variant_guids = []
    for var in variants_json:
        for family_guid in var['familyGuids']:
            saved_variant = saved_variants_map.get((var['variantId'], family_guid))
            if saved_variant:
                update_model_from_json(saved_variant, {'saved_variant_json': var}, user)
                updated_saved_variant_guids.append(saved_variant.guid)

    return updated_saved_variant_guids


def reset_cached_search_results(project, reset_index_metadata=False):
    try:
        redis_client = redis.StrictRedis(host=REDIS_SERVICE_HOSTNAME, socket_connect_timeout=3)
        keys_to_delete = []
        if project:
            result_guids = [res.guid for res in VariantSearchResults.objects.filter(families__project=project)]
            for guid in result_guids:
                keys_to_delete += redis_client.keys(pattern='search_results__{}*'.format(guid))
        else:
            keys_to_delete = redis_client.keys(pattern='search_results__*')
        if reset_index_metadata:
            keys_to_delete += redis_client.keys(pattern='index_metadata__*')
        if keys_to_delete:
            redis_client.delete(*keys_to_delete)
            logger.info('Reset {} cached results'.format(len(keys_to_delete)))
        else:
            logger.info('No cached results to reset')
    except Exception as e:
        logger.error("Unable to reset cached search results: {}".format(e))


def get_variant_key(xpos=None, ref=None, alt=None, genomeVersion=None, **kwargs):
    return '{}-{}-{}_{}'.format(xpos, ref, alt, genomeVersion)


def _saved_variant_genes(variants):
    gene_ids = set()
    for variant in variants:
        if isinstance(variant, list):
            for compound_het in variant:
                gene_ids.update(list(compound_het.get('transcripts', {}).keys()))
        else:
            gene_ids.update(list(variant.get('transcripts', {}).keys()))
    genes = get_genes_for_variants(gene_ids)
    for gene in genes.values():
        if gene:
            gene['locusListGuids'] = []
    return genes


def _add_locus_lists(projects, genes, add_list_detail=False, user=None, is_analyst=None):
    locus_lists = LocusList.objects.filter(projects__in=projects)

    if add_list_detail:
        locus_lists_by_guid = {
            ll['locusListGuid']: dict(intervals=[], **ll)
            for ll in get_json_for_locus_lists(locus_lists, user, is_analyst=is_analyst)
        }
    else:
        locus_lists_by_guid = defaultdict(lambda: {'intervals': []})
    intervals = LocusListInterval.objects.filter(locus_list__in=locus_lists)
    for interval in _get_json_for_models(intervals, nested_fields=[{'fields': ('locus_list', 'guid')}]):
        locus_lists_by_guid[interval['locusListGuid']]['intervals'].append(interval)

    for locus_list_gene in LocusListGene.objects.filter(locus_list__in=locus_lists, gene_id__in=genes.keys()).prefetch_related('locus_list', 'palocuslistgene'):
        gene_json = genes[locus_list_gene.gene_id]
        locus_list_guid = locus_list_gene.locus_list.guid
        gene_json['locusListGuids'].append(locus_list_guid)
        if hasattr(locus_list_gene, 'palocuslistgene'):
            if not gene_json.get('locusListConfidence'):
                gene_json['locusListConfidence'] = {}
            gene_json['locusListConfidence'][locus_list_guid] = locus_list_gene.palocuslistgene.confidence_level

    return locus_lists_by_guid


def _get_rna_seq_outliers(gene_ids, families):
    data_by_individual_gene = defaultdict(lambda: {'outliers': {}})

    outlier_data = get_json_for_rna_seq_outliers(
        RnaSeqOutlier.objects.filter(
            gene_id__in=gene_ids, p_adjust__lt=RnaSeqOutlier.SIGNIFICANCE_THRESHOLD, sample__individual__family__in=families),
        nested_fields=[{'fields': ('sample', 'individual', 'guid'), 'key': 'individualGuid'},]
    )
    for data in outlier_data:
        data_by_individual_gene[data.pop('individualGuid')]['outliers'][data['geneId']] = data

    return data_by_individual_gene


def _add_family_rna_tpm(families_by_guid):
    tpm_families = RnaSeqTpm.objects.filter(
        sample__individual__family__guid__in=families_by_guid.keys()
    ).values_list('sample__individual__family__guid', flat=True).distinct()
    for family_guid in tpm_families:
        families_by_guid[family_guid]['hasRnaTpmData'] = True


def _add_discovery_tags(variants, discovery_tags):
    for variant in variants:
        tags = discovery_tags.get(get_variant_key(**variant))
        if tags:
            if not variant.get('discoveryTags'):
                variant['discoveryTags'] = []
            variant['discoveryTags'] += [tag for tag in tags if tag['savedVariant']['familyGuid'] not in variant['familyGuids']]


LOAD_PROJECT_TAG_TYPES_CONTEXT_PARAM = 'loadProjectTagTypes'
LOAD_FAMILY_CONTEXT_PARAM = 'loadFamilyContext'

def get_variants_response(request, saved_variants, response_variants=None, add_all_context=False, include_igv=True,
                          add_locus_list_detail=False, include_rna_seq=True, include_missing_variants=False):
    response = get_json_for_saved_variants_with_tags(saved_variants, add_details=True, include_missing_variants=include_missing_variants)

    variants = list(response['savedVariantsByGuid'].values()) if response_variants is None else response_variants

    loaded_family_guids = set()
    for variant in variants:
        loaded_family_guids.update(variant['familyGuids'])
    families = Family.objects.filter(guid__in=loaded_family_guids).prefetch_related('project')
    projects = {family.project for family in families}
    project = list(projects)[0] if len(projects) == 1 else None

    discovery_tags = None
    is_analyst = user_is_analyst(request.user)
    if is_analyst:
        discovery_tags, discovery_response = get_json_for_discovery_tags(response['savedVariantsByGuid'].values())
        response.update(discovery_response)

    genes = _saved_variant_genes(variants)
    response['locusListsByGuid'] = _add_locus_lists(
        projects, genes, add_list_detail=add_locus_list_detail, user=request.user, is_analyst=is_analyst)

    if discovery_tags:
        _add_discovery_tags(variants, discovery_tags)
    response['genesById'] = genes

    if add_all_context or request.GET.get(LOAD_PROJECT_TAG_TYPES_CONTEXT_PARAM) == 'true':
        response['projectsByGuid'] = {project.guid: {'projectGuid': project.guid} for project in projects}
        add_project_tag_types(response['projectsByGuid'])

    if add_all_context or request.GET.get(LOAD_FAMILY_CONTEXT_PARAM) == 'true':
        add_families_context(
            response, families, project_guid=project.guid if project else None, user=request.user, is_analyst=is_analyst,
            has_case_review_perm=bool(project) and has_case_review_permissions(project, request.user), include_igv=include_igv,
        )

    if include_rna_seq:
        response['rnaSeqData'] = _get_rna_seq_outliers(genes.keys(), families)
        families_by_guid = response.get('familiesByGuid')
        if families_by_guid:
            _add_family_rna_tpm(families_by_guid)

    return response
