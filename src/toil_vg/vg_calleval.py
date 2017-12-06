#!/usr/bin/env python2.7
"""
vg_calleval.py: Compare vcfs with vcfeval.  Option to make freebayes calls to use as baseline.  Can
run on vg_mapeval.py output. 
"""
from __future__ import print_function
import argparse, sys, os, os.path, errno, random, subprocess, shutil, itertools, glob, tarfile
import doctest, re, json, collections, time, timeit
import logging, logging.handlers, SocketServer, struct, socket, threading
import string, math
import urlparse
import getpass
import pdb
import gzip
import logging
import copy
from collections import Counter

from math import ceil
from subprocess import Popen, PIPE

try:
    import numpy as np
    from sklearn.metrics import roc_auc_score, average_precision_score, r2_score, roc_curve
    have_sklearn = True
except:
    have_sklearn = False

import tsv
import vcf

from toil.common import Toil
from toil.job import Job
from toil.realtimeLogger import RealtimeLogger
from toil_vg.vg_common import *
from toil_vg.vg_call import chunked_call_parse_args, run_all_calling
from toil_vg.vg_vcfeval import vcfeval_parse_args, run_vcfeval
from toil_vg.context import Context, run_write_info_to_outstore

logger = logging.getLogger(__name__)

def calleval_subparser(parser):
    """
    Create a subparser for calleval.  Should pass in results of subparsers.add_parser()
    """

    # Add the Toil options so the job store is the first argument
    Job.Runner.addToilOptions(parser)
    
    # Add the out_store
    # TODO: do this at a higher level?
    # Or roll into Context?
    parser.add_argument('out_store',
                        help='output store.  All output written here. Path specified using same syntax as toil jobStore')

    parser.add_argument("--chroms", nargs='+', required=True,
                        help="Name(s) of reference path in graph(s) (separated by space)."
                        " Must be same length/order as --gams")
    # todo: move to chunked_call_parse_args and share with toil-vg run
    parser.add_argument("--gams", nargs='+', required=True, type=make_url,
                        help="GAMs to call.  One per chromosome. Must be same length/order as --chroms")
    parser.add_argument("--sample_name", type=str, required=True,
                        help="sample name (ex NA12878)")


    # Add common options shared with everybody
    add_common_vg_parse_args(parser)

    # Add common call options shared with toil_vg pipeline
    chunked_call_parse_args(parser)
    
    # Add common vcfeval options shared with toil_vg pipeline
    vcfeval_parse_args(parser)

    # Add common calleval options shared with toil_vg pipeline
    calleval_parse_args(parser)

    # Add common docker options shared with toil_vg pipeline
    add_container_tool_parse_args(parser)
    
def calleval_parse_args(parser):
    """
    Add the calleval options to the given argparse parser.
    """
    parser.add_argument('--gam_names', nargs='+', required=True,
                        help='names of vg runs (corresponds to gams and xg_paths)')
    parser.add_argument('--xg_paths', nargs='+', required=True, type=make_url,
                        help='xg indexes for the different graphs')
    parser.add_argument('--freebayes', action='store_true',
                        help='run freebayes as a baseline')
    parser.add_argument('--bam_names', nargs='+',
                        help='names of bwa runs (corresponds to bams)')
    parser.add_argument('--bams', nargs='+', type=make_url,
                        help='bam inputs for freebayes')                         
        
def validate_calleval_options(options):
    """
    Throw an error if an invalid combination of options has been selected.
    """
    require(len(options.gam_names) == len(options.xg_paths) == len(options.gams),
            '--gam_names, --xg_paths, --gams must all contain same number of elements')
    if options.freebayes:
        require(options.bams, '--bams must be given for use with freebayes')
    if options.bams or options.bam_names:
        require(options.bams and options.bam_names and len(options.bams) == len(options.bam_names),
                '--bams and --bam_names must be same length')
    # todo: generalize.  
    require(options.chroms and len(options.chroms) == 1,
            'one sequence must be specified with --chroms')
    require(options.vcfeval_baseline, '--vcfeval_baseline required')
    require(options.vcfeval_fasta, '--vcfeval_fasta required')
    require(not options.vcfeval_fasta.endswith('.gz'), 'gzipped fasta not currently supported')


def run_freebayes(job, context, fasta_file_id, bam_file_id, sample_name, region, offset, freebayes_opts,
                  out_name):
    """
    run freebayes to make a vcf
    """

    # make a local work directory
    work_dir = job.fileStore.getLocalTempDir()

    # download the input
    fasta_path = os.path.join(work_dir, 'ref.fa')
    bam_path = os.path.join(work_dir, 'alignment.bam')
    job.fileStore.readGlobalFile(fasta_file_id, fasta_path)
    job.fileStore.readGlobalFile(bam_file_id, bam_path)

    # index the fasta
    index_cmd = ['samtools', 'faidx', os.path.basename(fasta_path)]
    context.runner.call(job, index_cmd, work_dir=work_dir)

    # index the bam (this should probably get moved upstream and into its own job)
    sort_bam_path = os.path.join(work_dir, 'sort.bam')
    sort_cmd = ['samtools', 'sort', os.path.basename(bam_path), '-o',
                os.path.basename(sort_bam_path), '-O', 'BAM']
    context.runner.call(job, sort_cmd, work_dir=work_dir)
    bam_index_cmd = ['samtools', 'index', os.path.basename(sort_bam_path)]
    context.runner.call(job, bam_index_cmd, work_dir=work_dir)

    # run freebayes
    fb_cmd = ['freebayes', '-f', os.path.basename(fasta_path), os.path.basename(sort_bam_path)]
    if freebayes_opts:
        fb_cmd += freebayes_opts

    if region:
        fb_cmd += ['-r', region]

    vcf_path = os.path.join(work_dir, '{}-raw.vcf'.format(out_name))
    with open(vcf_path, 'w') as out_vcf:
        context.runner.call(job, fb_cmd, work_dir=work_dir, outfile=out_vcf)

    context.write_output_file(job, vcf_path)

    vcf_fix_path = os.path.join(work_dir, '{}.vcf'.format(out_name))
    
    # apply offset and sample name
    vcf_reader = vcf.Reader(open(vcf_path))
    vcf_writer = vcf.Writer(open(vcf_fix_path, 'w'), vcf_reader)
    for record in vcf_reader:
        if offset:
            record.POS += int(offset)
        if sample_name:
            pass
        vcf_writer.write_record(record)
    vcf_writer.flush()
    vcf_writer.close()

    context.runner.call(job, ['bgzip', os.path.basename(vcf_fix_path)], work_dir = work_dir)
    context.runner.call(job, ['tabix', '-p', 'vcf', os.path.basename(vcf_fix_path) + '.gz'], work_dir = work_dir)

    return (context.write_output_file(job, vcf_fix_path + '.gz'),
            context.write_output_file(job, vcf_fix_path + '.gz.tbi'))

def run_calleval_results(job, context, names, vcf_tbi_pairs, eval_results):
    """ output the calleval results"""

    # make a local work directory
    work_dir = job.fileStore.getLocalTempDir()

    # make a simple tsv
    stats_path = os.path.join(work_dir, 'calleval_stats.tsv')
    with open(stats_path, 'w') as stats_file:
        for name, f1 in zip(names, eval_results):
            stats_file.write('{}\t{}\n'.format(name, f1))

    return context.write_output_file(job, stats_path)
                             
        
def run_calleval(job, context, xg_ids, gam_ids, bam_ids, gam_names, bam_names,
                 vcfeval_baseline_id, vcfeval_baseline_tbi_id, fasta_id, bed_id,
                 genotype, sample_name, chrom, vcf_offset):
    """ top-level call-eval function.  runs the caller and genotype on every gam,
    and freebayes on every bam.  the resulting vcfs are put through vcfeval
    and the accuracies are tabulated in the output
    """
    vcf_tbi_id_pairs = [] 
    names = []
    eval_results = []
    if bam_ids:
        for bam_id, bam_name in zip(bam_ids, bam_names):
            fb_job = job.addChildJobFn(run_freebayes, context, fasta_id, bam_id, sample_name, chrom, vcf_offset,
                                       None, out_name = bam_name,
                                       cores=context.config.calling_cores,
                                       memory=context.config.calling_mem,
                                       disk=context.config.calling_disk)

            eval_job = fb_job.addFollowOnJobFn(run_vcfeval, context, sample_name, fb_job.rv(),
                                               vcfeval_baseline_id, vcfeval_baseline_tbi_id, 'ref.fasta',
                                               fasta_id, None, out_name=bam_name)
            vcf_tbi_id_pairs.append(fb_job.rv())            
            names.append(bam_name)            
            eval_results.append(eval_job.rv())

    if gam_ids:
        for gam_id, gam_name, xg_id in zip(gam_ids, gam_names, xg_ids):
            call_job = job.addChildJobFn(run_all_calling, context, xg_id, [gam_id], [chrom], [vcf_offset],
                                         sample_name, genotype, out_name=gam_name,
                                         cores=context.config.misc_cores,
                                         memory=context.config.misc_mem,
                                         disk=context.config.misc_disk)
            
            
            eval_job = call_job.addFollowOnJobFn(run_vcfeval, context, sample_name, call_job.rv(),
                                                 vcfeval_baseline_id, vcfeval_baseline_tbi_id, 'ref.fasta',
                                                 fasta_id, None, out_name=gam_name)
            names.append(gam_name)            
            vcf_tbi_id_pairs.append(call_job.rv())
            eval_results.append(eval_job.rv())

    calleval_results = job.addFollowOnJobFn(run_calleval_results, context, names, vcf_tbi_id_pairs, eval_results,
                                            cores=context.config.misc_cores,
                                            memory=context.config.misc_mem,
                                            disk=context.config.misc_disk).rv()

    return calleval_results, names, vcf_tbi_id_pairs, eval_results

def calleval_main(context, options):
    """ entrypoint for calling """

    validate_calleval_options(options)
            
    # How long did it take to run the entire pipeline, in seconds?
    run_time_pipeline = None
        
    # Mark when we start the pipeline
    start_time_pipeline = timeit.default_timer()
    
    with context.get_toil(options.jobStore) as toil:
        if not toil.options.restart:

            start_time = timeit.default_timer()

            # Upload local files to the job store            
            inputXGFileIDs = []
            xgToID = {}
            if options.xg_paths:
                for xg_path in options.xg_paths:
                    # we allow same files to be passed many times, but just import them once                    
                    if xg_path not in xgToID:
                        xgToID[xg_path] = toil.importFile(xg_path)
                    inputXGFileIDs.append(xgToID[xg_path])
            inputGamFileIDs = []
            gamToID = {}
            if options.gams:
                for gam in options.gams:
                    if gam not in gamToID:
                        gamToID[gam] = toil.importFile(gam)
                    inputGamFileIDs.append(gamToID[gam])
                        
            inputBamFileIDs = []
            if options.bams:
                for inputBamFileID in options.bams:
                    inputBamFileIDs.append(toil.importFile(inputBamFileID))

            vcfeval_baseline_id = toil.importFile(options.vcfeval_baseline)
            vcfeval_baseline_tbi_id = toil.importFile(options.vcfeval_baseline + '.tbi')
            fasta_id = toil.importFile(options.vcfeval_fasta)
            bed_id = toil.importFile(options.vcfeval_bed_regions) if options.vcfeval_bed_regions is not None else None

            end_time = timeit.default_timer()
            logger.info('Imported input files into Toil in {} seconds'.format(end_time - start_time))
            
            # Make a root job
            root_job = Job.wrapJobFn(run_calleval, context, inputXGFileIDs, inputGamFileIDs, inputBamFileIDs,
                                     options.gam_names, options.bam_names, 
                                     vcfeval_baseline_id, vcfeval_baseline_tbi_id, fasta_id, bed_id,
                                     options.genotype, 
                                     options.sample_name,
                                     options.chroms[0], options.vcf_offsets[0] if options.vcf_offsets else 0,
                                     cores=context.config.misc_cores,
                                     memory=context.config.misc_mem,
                                     disk=context.config.misc_disk)

            # Init the outstore
            init_job = Job.wrapJobFn(run_write_info_to_outstore, context, sys.argv)
            init_job.addFollowOn(root_job)            
            
            # Run the job and store the returned list of output files to download
            toil.start(init_job)
        else:
            toil.restart()
                
    end_time_pipeline = timeit.default_timer()
    run_time_pipeline = end_time_pipeline - start_time_pipeline
 
    print("All jobs completed successfully. Pipeline took {} seconds.".format(run_time_pipeline))
    
    
    