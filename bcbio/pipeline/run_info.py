"""Retrieve run information describing files to process in a pipeline.

This handles two methods of getting processing information: from a Galaxy
next gen LIMS system or an on-file YAML configuration.
"""
import collections
from contextlib import closing
import copy
import glob
import itertools
import operator
import os
import string

import toolz as tz
import yaml
from bcbio import install, utils, structural
from bcbio.bam import ref
from bcbio.log import logger
from bcbio.distributed import objectstore
from bcbio.illumina import flowcell
from bcbio.pipeline import alignment, config_utils, genome
from bcbio.pipeline import datadict as dd
from bcbio.provenance import diagnostics, programs, versioncheck
from bcbio.provenance import data as provenancedata
from bcbio.qc import viral
from bcbio.variation import annotation, effects, genotype, population, joint, vcfutils
from bcbio.variation.cortex import get_sample_name
from bcbio.bam.fastq import open_fastq

ALLOWED_CONTIG_NAME_CHARS = set(list(string.digits) + list(string.ascii_letters) + ["-", "_", "*", ":", "."])
ALGORITHM_NOPATH_KEYS = ["variantcaller", "realign", "recalibrate", "peakcaller",
                         "expression_caller",
                         "svcaller", "hetcaller", "jointcaller", "tools_off",
                         "mixup_check", "qc"]
ALGORITHM_FILEONLY_KEYS = ["custom_trim", "vcfanno"]

def organize(dirs, config, run_info_yaml, sample_names=None, add_provenance=True,
             integrations=None):
    """Organize run information from a passed YAML file or the Galaxy API.

    Creates the high level structure used for subsequent processing.

    sample_names is a list of samples to include from the overall file, for cases
    where we are running multiple pipelines from the same configuration file.
    """
    from bcbio.pipeline import qcsummary
    if integrations is None: integrations = {}
    logger.info("Using input YAML configuration: %s" % run_info_yaml)
    assert run_info_yaml and os.path.exists(run_info_yaml), \
        "Did not find input sample YAML file: %s" % run_info_yaml
    run_details = _run_info_from_yaml(dirs, run_info_yaml, config, sample_names,
                                      integrations=integrations)
    remote_retriever = None
    for iname, retriever in integrations.items():
        if iname in config:
            run_details = retriever.add_remotes(run_details, config[iname])
            remote_retriever = retriever
    out = []
    for item in run_details:
        item["dirs"] = dirs
        if "name" not in item:
            item["name"] = ["", item["description"]]
        elif isinstance(item["name"], basestring):
            description = "%s-%s" % (item["name"], clean_name(item["description"]))
            item["name"] = [item["name"], description]
            item["description"] = description
        # add algorithm details to configuration, avoid double specification
        item["resources"] = _add_remote_resources(item["resources"])
        item["config"] = config_utils.update_w_custom(config, item)
        item.pop("algorithm", None)
        item = add_reference_resources(item, remote_retriever)
        item["config"]["algorithm"]["qc"] = qcsummary.get_qc_tools(item)
        # Create temporary directories and make absolute, expanding environmental variables
        tmp_dir = tz.get_in(["config", "resources", "tmp", "dir"], item)
        if tmp_dir:
            # if no environmental variables, make and normalize the directory
            # otherwise we normalize later in distributed.transaction:
            if os.path.expandvars(tmp_dir) == tmp_dir:
                tmp_dir = utils.safe_makedir(os.path.expandvars(tmp_dir))
                tmp_dir = genome.abs_file_paths(tmp_dir, do_download=not integrations)
            item["config"]["resources"]["tmp"]["dir"] = tmp_dir
        out.append(item)
    out = _add_provenance(out, dirs, config, add_provenance)
    return out

def normalize_world(data):
    """Normalize a data object, useful after serializetion via CWL.
    """
    data = _normalize_files(data)
    return data

def _add_provenance(items, dirs, config, add_provenance=True):
    if add_provenance:
        p = programs.write_versions(dirs, config=config)
        d = provenancedata.write_versions(dirs, items)
        versioncheck.testall(items)
        p_db = diagnostics.initialize(dirs)
    out = []
    for item in items:
        if add_provenance:
            entity_id = diagnostics.store_entity(item)
            item["config"]["resources"]["program_versions"] = p
            item["provenance"] = {"programs": p, "entity": entity_id,
                                  "db": p_db, "data": d}
        out.append([item])
    return out

def setup_directories(work_dir, fc_dir, config, config_file):
    fastq_dir, galaxy_dir, config_dir = _get_full_paths(flowcell.get_fastq_dir(fc_dir)
                                                        if fc_dir else None,
                                                        config, config_file)
    # check default install for tool data if not found locally
    if not os.path.exists(os.path.join(galaxy_dir, "tool-data")):
        _, config_file = config_utils.load_system_config(work_dir=work_dir, allow_missing=True)
        if config_file and os.path.exists(os.path.join(os.path.dirname(config_file), "tool-data")):
            galaxy_dir = os.path.dirname(config_file)
    return {"fastq": fastq_dir, "galaxy": galaxy_dir,
            "work": work_dir, "flowcell": fc_dir, "config": config_dir}

def _get_full_paths(fastq_dir, config, config_file):
    """Retrieve full paths for directories in the case of relative locations.
    """
    if fastq_dir:
        fastq_dir = utils.add_full_path(fastq_dir)
    config_dir = utils.add_full_path(os.path.dirname(config_file))
    galaxy_config_file = utils.add_full_path(config.get("galaxy_config", "universe_wsgi.ini"),
                                             config_dir)
    return fastq_dir, os.path.dirname(galaxy_config_file), config_dir

# ## Remote resources

def _add_remote_resources(resources):
    """Retrieve remote resources like GATK/MuTect jars present in S3.
    """
    out = copy.deepcopy(resources)
    for prog, info in resources.items():
        for key, val in info.items():
            if key == "jar" and objectstore.is_remote(val):
                store_dir = utils.safe_makedir(os.path.join(os.getcwd(), "inputs", "jars", prog))
                fname = objectstore.download(val, store_dir, store_dir)
                version_file = os.path.join(store_dir, "version.txt")
                if not utils.file_exists(version_file):
                    version = install.get_gatk_jar_version(prog, fname)
                    with open(version_file, "w") as out_handle:
                        out_handle.write(version)
                else:
                    with open(version_file) as in_handle:
                        version = in_handle.read().strip()
                del out[prog][key]
                out[prog]["dir"] = store_dir
                out[prog]["version"] = version
    return out

# ## Genome reference information

def add_reference_resources(data, remote_retriever=None):
    """Add genome reference information to the item to process.
    """
    aligner = data["config"]["algorithm"].get("aligner", None)
    if remote_retriever:
        data["reference"] = remote_retriever.get_refs(data["genome_build"],
                                                      alignment.get_aligner_with_aliases(aligner),
                                                      data["config"])
    else:
        data["reference"] = genome.get_refs(data["genome_build"], aligner, data["dirs"]["galaxy"], data)
        _check_ref_files(data["reference"], data)
    # back compatible `sam_ref` target
    data["sam_ref"] = utils.get_in(data, ("reference", "fasta", "base"))
    ref_loc = utils.get_in(data, ("config", "resources", "species", "dir"),
                           utils.get_in(data, ("reference", "fasta", "base")))
    if remote_retriever:
        data = remote_retriever.get_resources(data["genome_build"], ref_loc, data)
    else:
        data["genome_resources"] = genome.get_resources(data["genome_build"], ref_loc, data)
    data["genome_resources"] = genome.add_required_resources(data["genome_resources"])
    if effects.get_type(data) == "snpeff" and "snpeff" not in data["reference"]:
        data["reference"]["snpeff"] = effects.get_snpeff_files(data)
    if "genome_context" not in data["reference"]:
        data["reference"]["genome_context"] = annotation.get_context_files(data)
    if "viral" not in data["reference"]:
        data["reference"]["viral"] = viral.get_files(data)
    if not data["reference"]["viral"]:
        data["reference"]["viral"] = None

    data = _fill_validation_targets(data)
    data = _fill_prioritization_targets(data)
    data = _fill_capture_regions(data)
    # Re-enable when we have ability to re-define gemini configuration directory
    if False:
        if population.do_db_build([data], need_bam=False):
            data["reference"]["gemini"] = population.get_gemini_files(data)
    return data

def _check_ref_files(ref_info, data):
    problems = []
    if not data["genome_build"]:
        problems.append("Did not find 'genome_build' for sample: %s" % dd.get_sample_name(data))
    elif not tz.get_in(["fasta", "base"], ref_info):
        problems.append("Did not find fasta reference file for genome %s.\n" % (data["genome_build"]) +
                        "Check tool-data/*.loc files to ensure paths to reference data are correct.")
    else:
        for contig in ref.file_contigs(ref_info["fasta"]["base"], data["config"]):
            cur_problems = set([])
            for char in list(contig.name):
                if char not in ALLOWED_CONTIG_NAME_CHARS:
                    cur_problems.add(char)
            if len(cur_problems) > 0:
                problems.append("Found non-allowed characters in chromosome name %s: %s" %
                                (contig.name, " ".join(list(cur_problems))))
    if len(problems) > 0:
        msg = ("\nProblems with input reference file %s\n" % tz.get_in(["fasta", "base"], ref_info))
        raise ValueError(msg + "\n".join(problems) + "\n")

def _fill_validation_targets(data):
    """Fill validation targets pointing to globally installed truth sets.
    """
    ref_file = dd.get_ref_file(data)
    sv_truth = tz.get_in(["config", "algorithm", "svvalidate"], data, {})
    sv_targets = (zip(itertools.repeat("svvalidate"), sv_truth.keys()) if isinstance(sv_truth, dict)
                  else ["svvalidate", sv_truth])
    for vtarget in [list(xs) for xs in [["validate"], ["validate_regions"]] + sv_targets]:
        val = tz.get_in(["config", "algorithm"] + vtarget, data)
        if val and not os.path.exists(val) and not objectstore.is_remote(val):
            installed_val = os.path.normpath(os.path.join(os.path.dirname(ref_file), os.pardir, "validation", val))
            if os.path.exists(installed_val):
                data = tz.update_in(data, ["config", "algorithm"] + vtarget, lambda x: installed_val)
            else:
                raise ValueError("Configuration problem. Validation file not found for %s: %s" %
                                 (vtarget, val))
    return data

def _fill_capture_regions(data):
    """Fill short-hand specification of BED capture regions.
    """
    special_targets = {"sv_regions": ("exons", "transcripts")}
    ref_file = dd.get_ref_file(data)
    for target in ["variant_regions", "sv_regions", "coverage"]:
        val = tz.get_in(["config", "algorithm", target], data)
        if val and not os.path.exists(val) and not objectstore.is_remote(val):
            installed_vals = []
            # Check prioritize directory
            for ext in [".bed", ".bed.gz"]:
                installed_vals += glob.glob(os.path.normpath(os.path.join(os.path.dirname(ref_file), os.pardir,
                                                                          "coverage", val + ext)))
            if len(installed_vals) == 0:
                if target not in special_targets or not val.startswith(special_targets[target]):
                    raise ValueError("Configuration problem. BED file not found for %s: %s" %
                                    (target, val))
            else:
                assert len(installed_vals) == 1, installed_vals
                data = tz.update_in(data, ["config", "algorithm", target], lambda x: installed_vals[0])
    return data

def _fill_prioritization_targets(data):
    """Fill in globally installed files for prioritization.
    """
    ref_file = dd.get_ref_file(data)
    for target in ["svprioritize", "coverage"]:
        val = tz.get_in(["config", "algorithm", target], data)
        if val and not os.path.exists(val) and not objectstore.is_remote(val):
            installed_vals = []
            # Check prioritize directory
            for ext in [".bed", ".bed.gz"]:
                installed_vals += glob.glob(os.path.normpath(os.path.join(os.path.dirname(ref_file), os.pardir,
                                                                          "coverage", "prioritize",
                                                                          val + "*%s" % ext)))
            # Check sv-annotation directory for prioritize gene name lists
            if target == "svprioritize":
                installed_vals += glob.glob(os.path.join(
                    os.path.dirname(os.path.realpath(utils.which("simple_sv_annotation.py"))),
                    "%s*" % os.path.basename(val)))
            if len(installed_vals) == 0:
                # some targets can be filled in later
                if target not in set(["coverage"]):
                    raise ValueError("Configuration problem. BED file not found for %s: %s" %
                                     (target, val))
                else:
                    installed_val = val
            elif len(installed_vals) == 1:
                installed_val = installed_vals[0]
            else:
                # check for partial matches
                installed_val = None
                for v in installed_vals:
                    if v.endswith(val + ".bed.gz") or v.endswith(val + ".bed"):
                        installed_val = v
                        break
                # handle date-stamped inputs
                if not installed_val:
                    installed_val = sorted(installed_vals, reverse=True)[0]
            data = tz.update_in(data, ["config", "algorithm", target], lambda x: installed_val)
    return data

# ## Sample and BAM read group naming


def _clean_metadata(data):
    batches = tz.get_in(("metadata", "batch"), data)
    # Ensure batches are strings and have no duplicates
    if batches:
        if isinstance(batches, (list, tuple)):
            batches = [_clean_characters(str(x)) for x in sorted(list(set(batches)))]
        else:
            batches = _clean_characters(str(batches))
        data["metadata"]["batch"] = batches
    # If we have jointcalling, add a single batch if not present
    elif tz.get_in(["algorithm", "jointcaller"], data):
        if "metadata" not in data:
            data["metadata"] = {}
        data["metadata"]["batch"] = "%s-joint" % dd.get_sample_name(data)
    return data

def _clean_algorithm(data):
    """Clean algorithm keys, handling items that can be specified as lists or single items.
    """
    # convert single items to lists
    for key in ["variantcaller", "jointcaller", "svcaller"]:
        val = tz.get_in(["algorithm", key], data)
        if val:
            if not isinstance(val, (list, tuple)) and isinstance(val, basestring):
                val = [val]
            # check for cases like [false] or [None]
            if isinstance(val, (list, tuple)):
                if len(val) == 1 and not val[0] or (isinstance(val[0], basestring)
                                                    and val[0].lower() in ["none", "false"]):
                    val = False
            data["algorithm"][key] = val
    return data

def _clean_background(data):
    """Clean up background specification, remaining back compatible.
    """
    allowed_keys = set(["variant", "cnv_reference"])
    val = tz.get_in(["algorithm", "background"], data)
    errors = []
    if val:
        out = {}
        # old style specification, single string for variant
        if isinstance(val, basestring):
            out["variant"] = _file_to_abs(val, [os.getcwd()])
        elif isinstance(val, dict):
            for k, v in val.items():
                if k in allowed_keys:
                    out[k] = _file_to_abs(v, [os.getcwd()])
                else:
                    errors.append("Unexpected key: %s" % k)
        else:
            errors.append("Unexpected input: %s" % val)
        if errors:
            raise ValueError("Problematic algorithm background specification for %s:\n %s" %
                             (data["description"], "\n".join(errors)))
        data["algorithm"]["background"] = out
    return data

def _clean_characters(x):
    """Clean problem characters in sample lane or descriptions.
    """
    for problem in [" ", ".", "/", "\\", "[", "]", "&", ";", "#", "+", ":", ")", "("]:
        x = x.replace(problem, "_")
    return x

def prep_rg_names(item, config, fc_name, fc_date):
    """Generate read group names from item inputs.
    """
    if fc_name and fc_date:
        lane_name = "%s_%s_%s" % (item["lane"], fc_date, fc_name)
    else:
        lane_name = item["description"]
    return {"rg": item["description"],
            "sample": item["description"],
            "lane": lane_name,
            "pl": (tz.get_in(["algorithm", "platform"], item)
                   or tz.get_in(["algorithm", "platform"], item, "illumina")).lower(),
            "lb": tz.get_in(["metadata", "library"], item),
            "pu": tz.get_in(["metadata", "platform_unit"], item) or lane_name}

# ## Configuration file validation

def _check_for_duplicates(xs, attr, check_fn=None):
    """Identify and raise errors on duplicate items.
    """
    dups = []
    for key, vals in itertools.groupby(x[attr] for x in xs):
        if len(list(vals)) > 1:
            dups.append(key)
    if len(dups) > 0:
        psamples = []
        for x in xs:
            if x[attr] in dups:
                psamples.append(x)
        # option to skip problem based on custom input function.
        if check_fn and check_fn(psamples):
            return
        descrs = [x["description"] for x in psamples]
        raise ValueError("Duplicate '%s' found in input sample configuration.\n"
                         "Required to be unique for a project: %s\n"
                         "Problem found in these samples: %s" % (attr, dups, descrs))

def _check_for_batch_clashes(xs):
    """Check that batch names do not overlap with sample names.
    """
    names = set([x["description"] for x in xs])
    dups = set([])
    for x in xs:
        batches = tz.get_in(("metadata", "batch"), x)
        if batches:
            if not isinstance(batches, (list, tuple)):
                batches = [batches]
            for batch in batches:
                if batch in names:
                    dups.add(batch)
    if len(dups) > 0:
        raise ValueError("Batch names must be unique from sample descriptions.\n"
                         "Clashing batch names: %s" % sorted(list(dups)))

def _check_for_problem_somatic_batches(items, config):
    """Identify problem batch setups for somatic calling.

    We do not support multiple tumors in a single batch and VarDict(Java) does not
    handle pooled calling, only tumor/normal.
    """
    to_check = []
    for data in items:
        data = copy.deepcopy(data)
        data["config"] = config_utils.update_w_custom(config, data)
        to_check.append(data)
    data_by_batches = collections.defaultdict(list)
    for data in to_check:
        batches = dd.get_batches(data)
        if batches:
            for batch in batches:
                data_by_batches[batch].append(data)
    for batch, items in data_by_batches.items():
        if vcfutils.get_paired(items):
            vcfutils.check_paired_problems(items)
        elif len(items) > 1:
            vcs = vcfutils.get_somatic_variantcallers(items)
            if "vardict" in vcs:
                raise ValueError("VarDict does not support pooled non-tumor/normal calling, in batch %s: %s"
                                 % (batch, [dd.get_sample_name(data) for data in items]))
            elif "mutect" in vcs or "mutect2" in vcs:
                raise ValueError("MuTect and MuTect2 require a 'phenotype: tumor' sample for calling, "
                                 "in batch %s: %s"
                                 % (batch, [dd.get_sample_name(data) for data in items]))

def _check_for_misplaced(xs, subkey, other_keys):
    """Ensure configuration keys are not incorrectly nested under other keys.
    """
    problems = []
    for x in xs:
        check_dict = x.get(subkey, {})
        for to_check in other_keys:
            if to_check in check_dict:
                problems.append((x["description"], to_check, subkey))
    if len(problems) > 0:
        raise ValueError("\n".join(["Incorrectly nested keys found in sample YAML. These should be top level:",
                                    " sample         |   key name      |   nested under ",
                                    "----------------+-----------------+----------------"] +
                                   ["% 15s | % 15s | % 15s" % (a, b, c) for (a, b, c) in problems]))

TOPLEVEL_KEYS = set(["description", "analysis", "genome_build", "metadata", "algorithm",
                     "resources", "files", "vrn_file", "lane", "upload", "rgnames"])
ALGORITHM_KEYS = set(["platform", "aligner", "bam_clean", "bam_sort",
                      "trim_reads", "trim_ends", "adapters", "custom_trim", "species", "kraken",
                      "align_split_size", "save_diskspace",
                      "transcriptome_align",
                      "quality_format", "write_summary", "merge_bamprep",
                      "coverage", "coverage_interval", "maxcov_downsample",
                      "ploidy", "indelcaller",
                      "variantcaller", "jointcaller", "variant_regions",
                      "peakcaller", "chip_method",
                      "effects", "effects_transcripts", "mark_duplicates",
                      "svcaller", "svvalidate", "svprioritize",
                      "hlacaller", "hlavalidate",
                      "sv_regions", "hetcaller", "recalibrate", "realign",
                      "phasing", "validate",
                      "validate_regions", "validate_genome_build", "validate_method",
                      "clinical_reporting", "nomap_split_size", "transcriptome_fasta",
                      "transcriptome_gtf",
                      "nomap_split_targets", "ensemble", "background",
                      "disambiguate", "strandedness", "fusion_mode", "fusion_caller",
                      "ericscript_db",
                      "min_read_length", "coverage_depth_min", "callable_min_size",
                      "min_allele_fraction", "umi_type", "minimum_barcode_depth",
                      "cellular_barcodes", "vcfanno",
                      "sample_barcodes",
                      "remove_lcr", "joint_group_size",
                      "archive", "tools_off", "tools_on", "transcript_assembler",
                      "mixup_check", "expression_caller", "qc", "positional_umi",
                      "cellular_barcode_correction",
                      "singlecell_quantifier", "spikein_fasta", "preseq",] +
                     # development
                     ["cwl_reporting"] +
                     # back compatibility
                      ["coverage_depth_max", "coverage_depth"])
ALG_ALLOW_BOOLEANS = set(["merge_bamprep", "mark_duplicates", "remove_lcr",
                          "clinical_reporting", "transcriptome_align",
                          "fusion_mode", "assemble_transcripts", "trim_reads",
                          "recalibrate", "realign", "cwl_reporting", "save_diskspace"])
ALG_ALLOW_FALSE = set(["aligner", "align_split_size", "bam_clean", "bam_sort",
                       "effects", "phasing", "mixup_check", "indelcaller",
                       "variantcaller", "positional_umi", "maxcov_downsample", "preseq"])

ALG_DOC_URL = "https://bcbio-nextgen.readthedocs.org/en/latest/contents/configuration.html#algorithm-parameters"

def _check_algorithm_keys(item):
    """Check for unexpected keys in the algorithm section.

    Needs to be manually updated when introducing new keys, but avoids silent bugs
    with typos in key names.
    """
    problem_keys = [k for k in item["algorithm"].iterkeys() if k not in ALGORITHM_KEYS]
    if len(problem_keys) > 0:
        raise ValueError("Unexpected configuration keyword in 'algorithm' section: %s\n"
                         "See configuration documentation for supported options:\n%s\n"
                         % (problem_keys, ALG_DOC_URL))

def _check_algorithm_values(item):
    """Check for misplaced inputs in the algorithms.

    - Identify incorrect boolean values where a choice is required.
    """
    problems = []
    for k, v in item.get("algorithm", {}).items():
        if v is True and k not in ALG_ALLOW_BOOLEANS:
            problems.append("%s set as true" % k)
        elif v is False and (k not in ALG_ALLOW_BOOLEANS and k not in ALG_ALLOW_FALSE):
            problems.append("%s set as false" % k)
    if len(problems) > 0:
        raise ValueError("Incorrect settings in 'algorithm' section for %s:\n%s"
                         "\nSee configuration documentation for supported options:\n%s\n"
                         % (item["description"], "\n".join(problems), ALG_DOC_URL))


def _check_toplevel_misplaced(item):
    """Check for algorithm keys accidentally placed at the top level.
    """
    problem_keys = [k for k in item.keys() if k in ALGORITHM_KEYS]
    if len(problem_keys) > 0:
        raise ValueError("Unexpected configuration keywords found in top level of %s: %s\n"
                         "This should be placed in the 'algorithm' section."
                         % (item["description"], problem_keys))
    problem_keys = [k for k in item.keys() if k not in TOPLEVEL_KEYS]
    if len(problem_keys) > 0:
        raise ValueError("Unexpected configuration keywords found in top level of %s: %s\n"
                         % (item["description"], problem_keys))

def _detect_fastq_format(in_file, MAX_RECORDS=1000):
    ranges = {"sanger": (33, 126),
              "solexa": (59, 126),
              "illumina_1.3+": (64, 126),
              "illumina_1.5+": (66, 126)}

    gmin, gmax = 99, 0
    possible = set(ranges.keys())

    with closing(open_fastq(in_file)) as in_handle:
        four = itertools.islice(in_handle, 3, None, 4)
        count = 0
        for line in four:
            if len(possible) == 1:
                return possible
            if count > MAX_RECORDS:
                break
            count += 1
            vals = [ord(c) for c in line.rstrip()]
            # if there is a short sequence, skip it
            if len(vals) < 20:
                continue
            lmin = min(vals)
            lmax = max(vals)
            for encoding, (emin, emax) in ranges.items():
                if encoding in possible:
                    if lmin < emin or lmax > emax:
                        possible.remove(encoding)

    return possible

def _check_quality_format(items):
    """
    Check if quality_format="standard" and fastq_format is not sanger
    """
    SAMPLE_FORMAT = {"illumina_1.3+": "illumina",
                     "illumina_1.5+": "illumina",
                     "illumina_1.8+": "standard",
                     "solexa": "solexa",
                     "sanger": "standard"}
    fastq_extensions = ["fq.gz", "fastq.gz", ".fastq", ".fq"]

    for item in items:
        specified_format = item["algorithm"].get("quality_format", "standard").lower()
        if specified_format not in SAMPLE_FORMAT.values():
            raise ValueError("Quality format specified in the YAML file"
                             "is not supported. Supported values are %s."
                             % (SAMPLE_FORMAT.values()))

        fastq_file = next((file for file in item.get('files') or [] if
                           any([ext for ext in fastq_extensions if ext in file])), None)

        if fastq_file and specified_format and not objectstore.is_remote(fastq_file):
            fastq_format = _detect_fastq_format(fastq_file)
            detected_encodings = set([SAMPLE_FORMAT[x] for x in fastq_format])
            if detected_encodings:
                if specified_format not in detected_encodings:
                    raise ValueError("Quality format specified in the YAML "
                                     "file might be a different encoding. "
                                     "'%s' was specified but possible formats "
                                     "detected were %s." % (specified_format,
                                                            ", ".join(detected_encodings)))


def _check_aligner(item):
    """Ensure specified aligner is valid choice.
    """
    allowed = set(alignment.TOOLS.keys() + [None, False])
    if item["algorithm"].get("aligner") not in allowed:
        raise ValueError("Unexpected algorithm 'aligner' parameter: %s\n"
                         "Supported options: %s\n" %
                         (item["algorithm"].get("aligner"), sorted(list(allowed))))

def _check_variantcaller(item):
    """Ensure specified variantcaller is a valid choice.
    """
    allowed = set(genotype.get_variantcallers().keys() + [None, False])
    vcs = item["algorithm"].get("variantcaller")
    if not isinstance(vcs, dict):
        vcs = {"variantcaller": vcs}
    for vc_set in vcs.values():
        if not isinstance(vc_set, (tuple, list)):
            vc_set = [vc_set]
        problem = [x for x in vc_set if x not in allowed]
        if len(problem) > 0:
            raise ValueError("Unexpected algorithm 'variantcaller' parameter: %s\n"
                             "Supported options: %s\n" % (problem, sorted(list(allowed))))
    # Ensure germline somatic calling only specified with tumor/normal samples
    if "germline" in vcs or "somatic" in vcs:
        paired = vcfutils.get_paired_phenotype(item)
        if not paired:
            raise ValueError("%s: somatic/germline calling in 'variantcaller' "
                             "but tumor/normal metadata phenotype not specified" % dd.get_sample_name(item))

def _check_svcaller(item):
    """Ensure the provide structural variant caller is valid.
    """
    allowed = set(reduce(operator.add, [d.keys() for d in structural._CALLERS.values()]) + [None, False])
    svs = item["algorithm"].get("svcaller")
    if not isinstance(svs, (list, tuple)):
        svs = [svs]
    problem = [x for x in svs if x not in allowed]
    if len(problem) > 0:
        raise ValueError("Unexpected algorithm 'svcaller' parameters: %s\n"
                         "Supported options: %s\n" % (" ".join(["'%s'" % x for x in problem]),
                                                      sorted(list(allowed))))

def _check_jointcaller(data):
    """Ensure specified jointcaller is valid.
    """
    allowed = set(joint.get_callers() + [None, False])
    cs = data["algorithm"].get("jointcaller", [])
    if not isinstance(cs, (tuple, list)):
        cs = [cs]
    problem = [x for x in cs if x not in allowed]
    if len(problem) > 0:
        raise ValueError("Unexpected algorithm 'jointcaller' parameter: %s\n"
                         "Supported options: %s\n" % (problem, sorted(list(allowed))))

def _check_indelcaller(data):
    c = data["algorithm"].get("indelcaller")
    if c and isinstance(c, (tuple, list)):
        raise ValueError("In sample %s, indelcaller specified as list. Can only be a single item: %s"
                         % (data["description"], str(c)))

def _check_hlacaller(data):
    supported_genomes = set(["hg38"])
    c = data["algorithm"].get("hlacaller")
    if c:
        if data["genome_build"] not in supported_genomes:
            raise ValueError("In sample %s, HLA caller specified but genome %s not in supported: %s" %
                             (data["description"], data["genome_build"], ", ".join(sorted(list(supported_genomes)))))

def _check_realign(data):
    """Check for realignment, which is not supported in GATK4
    """
    if "gatk4" not in data["algorithm"].get("tools_off", []) and not "gatk4" == data["algorithm"].get("tools_off"):
        if data["algorithm"].get("realign"):
            raise ValueError("In sample %s, realign specified but it is not supported for GATK4. "
                             "Realignment is generally not necessary for most variant callers." %
                             (dd.get_sample_name(data)))

def _check_trim(data):
    """Check for valid values for trim_reads.
    """
    trim = data["algorithm"].get("trim_reads")
    if trim:
        if trim == "fastp" and data["algorithm"].get("align_split_size") is not False:
            raise ValueError("In sample %s, `trim_reads: fastp` currently requires `align_split_size: false`" %
                             (dd.get_sample_name(data)))


def _check_sample_config(items, in_file, config):
    """Identify common problems in input sample configuration files.
    """
    logger.info("Checking sample YAML configuration: %s" % in_file)
    _check_quality_format(items)
    _check_for_duplicates(items, "lane")
    _check_for_duplicates(items, "description")
    _check_for_batch_clashes(items)
    _check_for_problem_somatic_batches(items, config)
    _check_for_misplaced(items, "algorithm",
                         ["resources", "metadata", "analysis",
                          "description", "genome_build", "lane", "files"])

    [_check_toplevel_misplaced(x) for x in items]
    [_check_algorithm_keys(x) for x in items]
    [_check_algorithm_values(x) for x in items]
    [_check_aligner(x) for x in items]
    [_check_variantcaller(x) for x in items]
    [_check_svcaller(x) for x in items]
    [_check_indelcaller(x) for x in items]
    [_check_jointcaller(x) for x in items]
    [_check_hlacaller(x) for x in items]
    [_check_realign(x) for x in items]
    [_check_trim(x) for x in items]

# ## Read bcbio_sample.yaml files

def _file_to_abs(x, dnames, makedir=False):
    """Make a file absolute using the supplied base directory choices.
    """
    if x is None or os.path.isabs(x):
        return x
    elif isinstance(x, basestring) and objectstore.is_remote(x):
        return x
    elif isinstance(x, basestring) and x.lower() == "none":
        return None
    else:
        for dname in dnames:
            if dname:
                normx = os.path.normpath(os.path.join(dname, x))
                if os.path.exists(normx):
                    return normx
                elif makedir:
                    utils.safe_makedir(normx)
                    return normx
        raise ValueError("Did not find input file %s in %s" % (x, dnames))

def _normalize_files(item, fc_dir=None):
    """Ensure the files argument is a list of absolute file names.
    Handles BAM, single and paired end fastq.
    """
    files = item.get("files")
    if files:
        if isinstance(files, basestring):
            files = [files]
        fastq_dir = flowcell.get_fastq_dir(fc_dir) if fc_dir else os.getcwd()
        files = [_file_to_abs(x, [os.getcwd(), fc_dir, fastq_dir]) for x in files]
        files = [x for x in files if x]
        _sanity_check_files(item, files)
        item["files"] = files
    return item

def _sanity_check_files(item, files):
    """Ensure input files correspond with supported
    """
    msg = None
    file_types = set([("bam" if x.endswith(".bam") else "fastq") for x in files if x])
    if len(file_types) > 1:
        msg = "Found multiple file types (BAM and fastq)"
    file_type = file_types.pop()
    if file_type == "bam":
        if len(files) != 1:
            msg = "Expect a single BAM file input as input"
    elif file_type == "fastq":
        if len(files) not in [1, 2] and item["analysis"].lower() != "scrna-seq":
            msg = "Expect either 1 (single end) or 2 (paired end) fastq inputs"
        if len(files) == 2 and files[0] == files[1]:
            msg = "Expect both fastq files to not be the same"
    if msg:
        raise ValueError("%s for %s: %s" % (msg, item.get("description", ""), files))

def validate_yaml(yaml_in, yaml_fn):
    """Check with yamllint the yaml syntaxes
    Looking for duplicate keys."""
    try:
        import yamllint.linter as linter
        from yamllint.config import YamlLintConfig
    except ImportError:
        return
    conf = """{"extends": "relaxed",
               "rules": {"trailing-spaces": {"level": "warning"},
                         "new-lines": {"level": "warning"},
                         "new-line-at-end-of-file": {"level": "warning"}}}"""
    if utils.file_exists(yaml_in):
        with open(yaml_in) as in_handle:
            yaml_in = in_handle.read()
    out = linter.run(yaml_in, YamlLintConfig(conf))

    for problem in out:
        msg = '%(fn)s:%(line)s:%(col)s: [%(level)s] %(msg)s' % {'fn': yaml_fn,
                                                                'line': problem.line,
                                                                'col': problem.column,
                                                                'level': problem.level,
                                                                'msg': problem.message}
        if problem.level == "error":
            raise ValueError(msg)

def _run_info_from_yaml(dirs, run_info_yaml, config, sample_names=None, integrations=None):
    """Read run information from a passed YAML file.
    """
    validate_yaml(run_info_yaml, run_info_yaml)
    with open(run_info_yaml) as in_handle:
        loaded = yaml.load(in_handle)
    fc_name, fc_date = None, None
    if dirs.get("flowcell"):
        try:
            fc_name, fc_date = flowcell.parse_dirname(dirs.get("flowcell"))
        except ValueError:
            pass
    global_config = {}
    global_vars = {}
    resources = {}
    integration_config = {}
    if isinstance(loaded, dict):
        global_config = copy.deepcopy(loaded)
        del global_config["details"]
        if "fc_name" in loaded:
            fc_name = loaded["fc_name"].replace(" ", "_")
        if "fc_date" in loaded:
            fc_date = str(loaded["fc_date"]).replace(" ", "_")
        global_vars = global_config.pop("globals", {})
        resources = global_config.pop("resources", {})
        for iname in ["arvados"]:
            integration_config[iname] = global_config.pop(iname, {})
        loaded = loaded["details"]
    if sample_names:
        loaded = [x for x in loaded if x["description"] in sample_names]

    if integrations:
        for iname, retriever in integrations.items():
            if iname in config:
                config[iname] = retriever.set_cache(config[iname])
                loaded = retriever.add_remotes(loaded, config[iname])

    run_details = []
    for i, item in enumerate(loaded):
        item = _normalize_files(item, dirs.get("flowcell"))
        if "lane" not in item:
            item["lane"] = str(i + 1)
        item["lane"] = _clean_characters(str(item["lane"]))
        if "description" not in item:
            if _item_is_bam(item):
                item["description"] = get_sample_name(item["files"][0])
            else:
                raise ValueError("No `description` sample name provided for input #%s" % (i + 1))
        item["description"] = _clean_characters(str(item["description"]))
        if "upload" not in item:
            upload = global_config.get("upload", {})
            # Handle specifying a local directory directly in upload
            if isinstance(upload, basestring):
                upload = {"dir": upload}
            if fc_name:
                upload["fc_name"] = fc_name
            if fc_date:
                upload["fc_date"] = fc_date
            upload["run_id"] = ""
            if upload.get("dir"):
                upload["dir"] = _file_to_abs(upload["dir"], [dirs.get("work")], makedir=True)
            item["upload"] = upload
        item["algorithm"] = _replace_global_vars(item["algorithm"], global_vars)
        item["algorithm"] = genome.abs_file_paths(item["algorithm"],
                                                  ignore_keys=ALGORITHM_NOPATH_KEYS,
                                                  fileonly_keys=ALGORITHM_FILEONLY_KEYS,
                                                  do_download=all(not x for x in integrations.values()))
        item["genome_build"] = str(item.get("genome_build", ""))
        item["algorithm"] = _add_algorithm_defaults(item["algorithm"])
        item["metadata"] = add_metadata_defaults(item.get("metadata", {}))
        item["rgnames"] = prep_rg_names(item, config, fc_name, fc_date)
        if item.get("files"):
            item["files"] = [genome.abs_file_paths(f, do_download=all(not x for x in integrations.values()))
                             for f in item["files"]]
        elif "files" in item:
            del item["files"]
        if item.get("vrn_file") and isinstance(item["vrn_file"], basestring):
            inputs_dir = utils.safe_makedir(os.path.join(dirs.get("work", os.getcwd()), "inputs",
                                                         item["description"]))
            item["vrn_file"] = genome.abs_file_paths(item["vrn_file"],
                                                     do_download=all(not x for x in integrations.values()))
            if os.path.isfile(item["vrn_file"]):
                item["vrn_file"] = vcfutils.bgzip_and_index(item["vrn_file"], config,
                                                            remove_orig=False, out_dir=inputs_dir)
            if not tz.get_in(("metadata", "batch"), item) and tz.get_in(["algorithm", "validate"], item):
                raise ValueError("%s: Please specify a metadata batch for variant file (vrn_file) input.\n" %
                                 (item["description"]) +
                                 "Batching with a standard sample provides callable regions for validation.")
        item = _clean_metadata(item)
        item = _clean_algorithm(item)
        item = _clean_background(item)
        # Add any global resource specifications
        if "resources" not in item:
            item["resources"] = {}
        for prog, pkvs in resources.items():
            if prog not in item["resources"]:
                item["resources"][prog] = {}
            if pkvs is not None:
                for key, val in pkvs.items():
                    item["resources"][prog][key] = val
        for iname, ivals in integration_config.items():
            if ivals:
                if iname not in item:
                    item[iname] = {}
                for k, v in ivals.items():
                    item[iname][k] = v

        run_details.append(item)
    _check_sample_config(run_details, run_info_yaml, config)
    return run_details

def _item_is_bam(item):
    files = item.get("files", [])
    return len(files) == 1 and files[0].endswith(".bam")

def add_metadata_defaults(md):
    """Central location for defaults for algorithm inputs.
    """
    defaults = {"batch": None,
                "phenotype": ""}
    for k, v in defaults.items():
        if k not in md:
            md[k] = v
    return md

def _add_algorithm_defaults(algorithm):
    """Central location specifying defaults for algorithm inputs.

    Converts allowed multiple inputs into lists if specified as a single item.
    Converts required single items into string if specified as a list
    """
    if not algorithm:
        algorithm = {}
    defaults = {"archive": None,
                "tools_off": [],
                "tools_on": [],
                "qc": [],
                "trim_reads": False,
                "adapters": [],
                "effects": "snpeff",
                "quality_format": "standard",
                "align_split_size": None,
                "bam_clean": False,
                "nomap_split_size": 250,
                "nomap_split_targets": 200,
                "mark_duplicates": False if not algorithm.get("aligner") else True,
                "coverage_interval": None,
                "recalibrate": False,
                "realign": False,
                "variant_regions": None,
                "validate": None,
                "validate_regions": None}
    convert_to_list = set(["tools_off", "tools_on", "hetcaller", "variantcaller", "qc", "disambiguate",
                           "vcfanno", "adapters", "custom_trim"])
    convert_to_single = set(["hlacaller", "indelcaller", "validate_method"])
    for k, v in defaults.items():
        if k not in algorithm:
            algorithm[k] = v
    for k, v in algorithm.items():
        if k in convert_to_list:
            if v and not isinstance(v, (list, tuple)) and not isinstance(v, dict):
                algorithm[k] = [v]
            # ensure dictionary specified inputs get converted into individual lists
            elif v and not isinstance(v, (list, tuple)) and isinstance(v, dict):
                new = {}
                for innerk, innerv in v.items():
                    if innerv and not isinstance(innerv, (list, tuple)) and not isinstance(innerv, dict):
                        innerv = [innerv]
                    new[innerk] = innerv
                algorithm[k] = new
            elif v is None:
                algorithm[k] = []
        elif k in convert_to_single:
            if v and not isinstance(v, basestring):
                if isinstance(v, (list, tuple)) and len(v) == 1:
                    algorithm[k] = v[0]
                else:
                    raise ValueError("Unexpected input in sample YAML; need a single item for %s: %s" % (k, v))
    return algorithm

def _replace_global_vars(xs, global_vars):
    """Replace globally shared names from input header with value.

    The value of the `algorithm` item may be a pointer to a real
    file specified in the `global` section. If found, replace with
    the full value.
    """
    if isinstance(xs, (list, tuple)):
        return [_replace_global_vars(x) for x in xs]
    elif isinstance(xs, dict):
        final = {}
        for k, v in xs.items():
            if isinstance(v, basestring) and v in global_vars:
                v = global_vars[v]
            final[k] = v
        return final
    else:
        return xs

def clean_name(xs):
    final = []
    safec = "_"
    for x in xs:
        if x not in string.ascii_letters + string.digits:
            if len(final) > 0 and final[-1] != safec:
                final.append(safec)
        else:
            final.append(x)
    if final[-1] == safec:
        final = final[:-1]
    return "".join(final)

def prep_system(run_info_yaml, bcbio_system=None):
    """Prepare system configuration information from an input configuration file.

    This does the work of parsing the system input file and setting up directories
    for use in 'organize'.
    """
    work_dir = os.getcwd()
    config, config_file = config_utils.load_system_config(bcbio_system, work_dir)
    dirs = setup_directories(work_dir, os.path.normpath(os.path.dirname(os.path.dirname(run_info_yaml))),
                             config, config_file)
    return [dirs, config, run_info_yaml]
