import sys
import os
import urllib2
import gzip
import biom
import shutil
from numpy import random as np_rand
from ete2 import NCBITaxa
from scripts.loggingwrapper import LoggingWrapper as logger
try:
    from configparser import ConfigParser
except ImportError:
    from ConfigParser import ConfigParser

ncbi = NCBITaxa()
RANKS = ['species', 'genus', 'family', 'order', 'class', 'phylum', 'superkingdom']
MAX_RANK = "family" # TODO
_log = None

"""
Reads a BIOM file and creates map of OTU: lineage, abundance
BIOM file format needs to have a taxonomy field in metadata which contains the taxonomy in the format:
RANK__SCINAME; LOWERRANK_LOWERSCINAME
"""
def read_taxonomic_profile(biom_profile, config, no_samples = None):
    table = biom.load_table(biom_profile)
    ids = table.ids(axis="observation")
    samples = table.ids()

    if no_samples is None:
        no_samples = len(samples)

    if no_samples is not None and no_samples != len(samples) and no_samples != 1:
        _log.warning("Number of samples (%s) does not match number of samples in biom file (%s)" % (no_samples, len(samples)))
        if no_samples > len(samples):
            no_samples = len(samples)
        _log.warning("Using the first %s samples" % no_samples)

    config.set("Main", "number_of_samples", str(no_samples))
    profile = {}
    for otu in ids:
        lineage = table.metadata(otu,axis="observation")["taxonomy"]
        try:
            lineage = lineage.split(";") # if no spaces
        except AttributeError:
            pass
        abundances = []
        for sample in samples[:no_samples]:
            abundances.append(table.get_value_by_ids(otu,sample))
        profile[otu] = (lineage, abundances)
    
    return profile

"""
Reads list of available genomes in the (tsv) format:
NCBI_ID Scientific_Name ftp_path
Additional files might be provided with:
NCBI_ID Scientific_Name genome_path
were path might either be online or offline/local
"""
def read_genomes_list(genomes_path, additional_file = None):
    genomes_map = {}
    with open(genomes_path,'r') as genomes:
        for line in genomes:
            ncbi_id, sci_name, ftp = line.strip().split('\t')
            http = ftp.replace("ftp://","http://") # not using ftp address but http (proxies)
            if ncbi_id in genomes_map:
                genomes_map[ncbi_id][1].append(http)
            else:
                genomes_map[ncbi_id] = (sci_name, [http]) # sci_name is always the same for same taxid (?)
    if additional_file is not None:
        with open(additional_file,'r') as add:
            for line in add:
                ncbi_id, sci_name, path = line.strip().split('\t')
                if ncbi_id in genomes_map:
                    genomes_map[ncbi_id][1].append(path)
                else:
                    genomes_map[ncbi_id] = (sci_name, [path]) # this might not be a http path
    return genomes_map

"""
Given all available genomes, creates a map sorted by ranks of available genomes on that particular rank, ordered by their ncbi ids
"""
def get_genomes_per_rank(genomes_map, ranks, max_rank):
    per_rank_map = {}
    for rank in ranks:
        if ranks.index(rank) > ranks.index(max_rank):
            break # only add genomes up to predefined rank
        per_rank_map[rank] = {}
    for genome in genomes_map:
        lineage = ncbi.get_lineage(genome) # this might contain some others ranks than ranks
        ranks = ncbi.get_rank(lineage)
        for tax_id in lineage: # go over the lineage
            if ranks[tax_id] in per_rank_map: # if we are a legal rank
                rank_map = per_rank_map[ranks[tax_id]]
                if tax_id in rank_map: # tax id already has a genome
                    rank_map[tax_id].append((genomes_map[genome][1][0],genome)) # add http address
                else:
                    rank_map[tax_id] = [(genomes_map[genome][1][0],genome)]
    return per_rank_map

"""
Given a BIOM lineage, create a NCBI tax id lineage
"""
def transform_lineage(lineage, ranks, max_rank):
    new_lineage = []
    for member in lineage:
        name = member.split("__")[-1] # name is on the right hand side
        if len(name) == 0:
            continue
        mapping = ncbi.get_name_translator([name])
        if name in mapping:
            taxid = mapping[name][0]
            if ncbi.get_rank([taxid])[taxid] in ranks:
                new_lineage.append(taxid) # should contain only one element
        else:
            name = name.split()[0]
            if name in mapping:
                taxid = mapping[name][0]
                if ncbi.get_rank([taxid])[taxid] in ranks:
                    new_lineage.append(taxid) # retry if space in name destroys ID
    return new_lineage[::-1] # invert list, so lowest rank appears first (last in BIOM)

"""
Given the OTU to lineage/abundances map and the genomes to lineage map, create map otu: taxid, genome, abundances
"""
def map_otus_to_genomes(profile, per_rank_map, ranks, max_rank, mu, sigma, max_strains, debug, replace, fillup):
    otu_genome_map = {}
    warnings = []
    for otu in profile:
        lin, abundances = profile[otu]
        lineage = transform_lineage(lin, ranks, max_rank)
        if len(lineage) == 0:
            warnings.append("No matching NCBI ID for otu %s, scientific name %s" % (otu, lin[-1].split("__")[-1]))
        lineage_ranks = ncbi.get_rank(lineage)
        for tax_id in lineage: # lineage sorted ascending
            rank = lineage_ranks[tax_id]
            if ranks.index(rank) > ranks.index(max_rank):
                warnings.append("Rank %s of OTU %s too high, no matching genomes found" % (rank, otu))
                warnings.append("Full lineage was %s, mapped from BIOM lineage %s" % (lineage, lin))
                break
            genomes = per_rank_map[rank]
            if tax_id not in genomes:
                warnings.append("For OTU %s no genomes have been found on rank %s with ID %s" % (otu, rank, tax_id))
                continue # warning will appear later if rank is too high
            available_genomes = genomes[tax_id]
            strains_to_draw = max((np_rand.geometric(2./max_strains) % max_strains),1)
            if len(available_genomes) >= strains_to_draw:
                used_indices = np_rand.choice(len(available_genomes),strains_to_draw,replace=False)
                used_genomes = set([available_genomes[i] for i in used_indices])
            else:
                used_genomes = set(available_genomes) # if not enough genomes: use all
            log_normal_vals = np_rand.lognormal(mu,sigma, len(used_genomes))
            sum_log_normal = sum(log_normal_vals)
            i = 0
            for path, genome_id in used_genomes:
                otu_id = otu + "." + str(i)
                otu_genome_map[otu_id] = (tax_id, genome_id, path, []) # taxid, genomeid, http path, abundances per sample
                relative_abundance = log_normal_vals[i]/sum_log_normal
                i += 1
                for abundance in abundances: # calculate abundance per sample
                    current_abundance = relative_abundance * abundance
                    otu_genome_map[otu_id][-1].append(current_abundance)
                if (not replace): # sampling without replacement:
                    for new_rank in per_rank_map:
                        for taxid in per_rank_map[new_rank]:
                            if (path, genome_id) in per_rank_map[new_rank][taxid]:
                                per_rank_map[new_rank][taxid].remove((path,genome_id))
            break # genome(s) found: we can break
    if len(warnings) > 0:
        _log.warning("Some OTUs could not be mapped")
        if debug:
            for warning in warnings:
                _log.warning(warning)
    return otu_genome_map


"""
Take fasta input file and split by any N occurence (and remove Ns)
"""
def split_by_N(fasta_path, out_path):
    os.system("scripts/split_fasta.pl %s %s" % (fasta_path, out_path))
    os.remove(fasta_path)

"""
Downloads the given genome and returns the out path
"""
def download_genome(genome, out_path):
    genome_path = os.path.join(out_path,"genomes")
    out_name = genome.rstrip().split('/')[-1]
    http_address = os.path.join(genome, out_name + "_genomic.fna.gz")
    opened = urllib2.urlopen(http_address)
    out = os.path.join(genome_path, out_name + ".fa")
    tmp_out = os.path.join(genome_path, out_name + "tmp.fa")
    out_gz = out + ".gz"
    with open(out_gz,'wb') as outF:
        outF.write(opened.read())
    gf = gzip.open(out_gz)
    new_out = open(tmp_out,'wb')
    new_out.write(gf.read())
    gf.close()
    os.remove(out_gz)
    new_out.close()
    split_by_N(tmp_out, out)
    return out

"""
Given the created maps and the old config files, creates the required files and new config
"""
def write_config(otu_genome_map, out_path, config):
    genome_to_id = os.path.join(out_path, "genome_to_id.tsv")
    config.set('community0','id_to_genome_file', genome_to_id)
    metadata = os.path.join(out_path, "metadata.tsv")
    with open(metadata,'w') as md:
        md.write("genome_ID\tOTU\tNCBI_ID\tnovelty_category\n") # write header
    config.set('community0','metadata',metadata)
    no_samples = int(config.get("Main","number_of_samples"))
    abundances = [os.path.join(out_path,"abundance%s.tsv" % i) for i in xrange(no_samples)]
    _log.info("Downloading %s genomes" % len(otu_genome_map))
    
    create_path = os.path.join(out_path,"genomes")
    if not os.path.exists(create_path):
        os.makedirs(create_path)
    for otu in otu_genome_map:
        taxid, genome_id, path, curr_abundances = otu_genome_map[otu]
        counter = 0
        while counter < 10:
            try:
                if path.startswith('http') or path.startswith('ftp'):
                    genome_path = download_genome(path, out_path)
                else:
                    out_name = path.rstrip().split('/')[-1]
                    genome_path = os.path.join(create_path, out_name)
                    shutil.copy2(path, genome_path)
                break
            except Exception as e:
                error = e
                counter += 1
        if counter == 10:
            _log.error("Caught exception %s while moving/downloading genomes" % e)
            _log.error("Genome %s (from %s) could not be downloaded after 10 tries, check your connection settings" % (otu, genome))
        with open(genome_to_id,'ab') as gid:
            gid.write("%s\t%s\n" % (otu, genome_path))
        with open(metadata,'ab') as md:
            md.write("%s\t%s\t%s\t%s\n" % (otu,taxid,genome_id,"new_strain"))
        i = 0
        for abundance in abundances:
            with open(abundance, 'ab') as ab:
                ab.write("%s\t%s\n" % (otu,curr_abundances[i]))
            i += 1
    abundance_files = ""
    for abundance in abundances[:-1]:
        abundance_files += abundance
        abundance_files += ","
    abundance_files += abundances[-1] # write csv of abundance files
    config.set("Main", 'distribution_file_paths', abundance_files)
    config.set("community0", "num_real_genomes", str(len(otu_genome_map)))
    config.set("community0", "genomes_total", str(len(otu_genome_map)))

    cfg_path = os.path.join(out_path, "config.ini")
    with open(cfg_path, 'wb') as cfg:
        config.write(cfg)
    return cfg_path

def fill_up(otu_genome_map, per_rank_map, tax_profile):
    abundances = dict()
    per_rank_map[new_rank][taxid].remove((path,genome_id))
    all_genomes = []
    for rank in per_rank_map:
        for taxid in per_rank_map[rank]:
            path, genome_id = per_rank_map[rank][taxid]
            all_genomes.append([taxid, genome_id, path])
    for otu in tax_profile:
        lin, curr_abundances = tax_profile[otu]
        abundances[otu] = sum(curr_abundances)/len(curr_abundances)
    sorted_ab = sorted(abundances.items(), key = lambda l:(l[1],l[0])) # sort by value
    for otu in sorted_ab:
        if otu not in otu_genome_map and len(all_genomes) > 1:
            next_genome = all_genomes[0]
            next_genome.append(tax_profile[otu][1])
            otu_genome_map[otu] = next_genome
            all_genomes = all_genomes[1:]

def generate_input(args):
    global _log
    _log = logger(verbose = args.debug)
    np_rand.seed(args.seed)
    config = ConfigParser()
    config.read(args.config)
    try:
        max_strains = int(config.get("Main", max_strains_per_otu))
    except:
        max_strains = 3 # no max_strains have been set for this community - use cami value
        _log.warning("Max strains per OTU not set, using default (3)")
    try:
        mu = int(config.get("Main", "log_mu"))
        sigma = int(config.get("Main", "log_sigma"))
    except:
        mu = 1
        sigma = 2 # this aint particularily beatiful
        _log.warning("Mu and sigma have not been set, using defaults (1,2)") #TODO 
    tax_profile = read_taxonomic_profile(args.profile, config, args.samples)
    genomes_map = read_genomes_list(args.reference_genomes, args.additional_references)
    per_rank_map = get_genomes_per_rank(genomes_map, RANKS, MAX_RANK)
    otu_genome_map = map_otus_to_genomes(tax_profile, per_rank_map, RANKS, MAX_RANK, mu, sigma, max_strains, args.debug, args.no_replace)
    if args.f:
        fill_up(otu_genome_map, per_rank_map, tax_profile)
    cfg_path = write_config(otu_genome_map, args.o, config)
    _log = None
    return cfg_path

