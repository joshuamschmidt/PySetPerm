import pandas as pd
import pyranges as pr
import numpy as np
import concurrent.futures as cf
from itertools import repeat
from scipy.stats import rankdata
from scipy.sparse import csr_matrix
import time
from random import sample

# --- global functions
def permutation_fset_intersect(args):
    permutation_array = args[0]
    function_array = args[1]
    max_z = max(permutation_array.max(), function_array.max()) + 1
    def csr_sparse(a, z):
        m, n = a.shape
        indptr = np.arange(0, m * n + 1, n)
        data = np.ones(m * n, dtype=np.uint16)
        return csr_matrix((data, a.ravel(), indptr), shape=(m, z))
    intersection = csr_sparse(permutation_array, max_z) * csr_sparse(function_array, max_z).T
    intersection = intersection.todense()
    return np.squeeze(np.asarray(intersection))


def listnp_to_padded_nparray(listnp):
    max_width = np.max([np.size(sublist) for sublist in listnp])
    padded_array = np.asarray(
        [np.pad(sublist, (0, max_width - np.size(sublist)), mode='constant', constant_values=(0, 0))
         for sublist
         in listnp])
    return padded_array.astype('uint16')


def annotation_sets_to_array(annotation, features, min_size=3):
    sets = annotation.join(features.set_index('feature'), on='feature').groupby('id')['idx'].apply(list)
    set_array = [s for s in sets if len(s) >= min_size]
    set_array = np.sort(listnp_to_padded_nparray(set_array))
    set_names = [i for i, s in enumerate(sets) if len(s) >= min_size]
    set_names = sets.index[set_names]
    return set_array, set_names


def sample_from_feature_list(feature_list, n_total, sample_ratio):
    out = pd.unique([item for sublist in sample(feature_list, int(round(n_total*sample_ratio,0))) for item in sublist])
    while len(out) < n_total:
        out = np.append(out, pd.unique([item for sublist in sample(feature_list, n_total) for item in sublist]))
        out = pd.unique(out)
    out = out[:n_total]
    #out = np.sort(out)
    return out.astype('uint16')


def array_of_resamples_tup(args):
    feature_list, n_total, n_reps = args[0], args[1], args[2]
    out = np.ndarray((n_reps, n_total), dtype='uint16')
    for i in range(n_reps):
        out[i] = sample_from_feature_list(feature_list, n_total, 1.4)
    return out


def n_jobs_core_list(n_reps, n_cores):
    quotient, remainder = divmod(n_reps, n_cores)
    n_per_core = [quotient] * n_cores
    for i in range(remainder):
        n_per_core[i] = n_per_core[i] + 1
    return n_per_core


def multicore_resample(n_features, n_reps, n_cores, feature_list):
    n_per_core = n_jobs_core_list(n_reps, n_cores)
    with cf.ProcessPoolExecutor(max_workers=n_cores) as executor:
        results = executor.map(array_of_resamples_tup, zip(repeat(feature_list), repeat(n_features), n_per_core))
    results = list(results)
    return np.concatenate(results)


def multicore_intersect(permutation_array, functionalset_array, n_cores):
    split_permutation_array = np.array_split(permutation_array, n_cores)
    with cf.ProcessPoolExecutor(max_workers=n_cores) as executor:
        results = executor.map(permutation_fset_intersect, zip(split_permutation_array, repeat(functionalset_array)))
    results = list(results)
    return np.concatenate(results)


def calculate_p_values(c_set_n, p_set_n):
    p_e = []
    p_d = []
    n_perm = p_set_n.shape[0]
    if n_perm == 1:
        #p_e.append((np.size(np.where(p_set_n >= c_set_n)) + 1) / (n_perm + 1))
        #p_d.append((np.size(np.where(p_set_n <= c_set_n)) + 1) / (n_perm + 1))
        raise ValueError("can only calculate p-val;ues if there is more than one permutation!")
    else:
        if(len(p_set_n.shape)>1):
            for i in range(p_set_n.shape[1]):
                p_e.append((np.size(np.where(p_set_n[:, i] >= c_set_n[i])) + 1) / (n_perm + 1))
                p_d.append((np.size(np.where(p_set_n[:, i] <= c_set_n[i])) + 1) / (n_perm + 1))
        if(len(p_set_n.shape)==1):
            p_e.append( (np.size(np.where(p_set_n >= c_set_n)) + 1) / (n_perm + 1) )
            p_d.append( (np.size(np.where(p_set_n <= c_set_n)) + 1) / (n_perm + 1) )
    return p_e, p_d


def make_results_table(test_obj, function_obj, set_perm_obj, annotation_obj):
    out = function_obj.function_sets.groupby('Id', as_index=False).agg({'FunctionName': pd.Series.unique})
    out = out[out['Id'].isin(function_obj.function_array2d_ids)]
    out['obs_n'] = test_obj.n_candidate_per_function
    out['perm_mean_n'] = set_perm_obj.mean_per_set
    sem=set_perm_obj.sd_per_set / np.sqrt(set_perm_obj.n_permutations)
    out['perm_sem'] = sem
    e_array = np.asarray(out['obs_n'] / out['perm_mean_n'].values)
    sem_array = e_array * np.sqrt(np.square(2*sem/set_perm_obj.mean_per_set))
    log_e = np.log2(e_array, out=np.empty((np.shape(e_array)[0],)) * np.nan, where=(e_array!=0))
    out['enrich(log2)'] = log_e
    out['u_95%(CI)'] = np.log2(e_array+sem_array, out=np.empty((np.shape(sem_array)[0],)) * np.nan, where=(e_array+sem_array!=0))
    out['l_95%(CI)'] = np.log2(e_array-sem_array, out=np.empty((np.shape(sem_array)[0],)) * np.nan, where=(e_array-sem_array!=0))
    out['p_enrich'] = set_perm_obj.p_enrichment
    out['fdr_enrich'] = fdr_from_p_matrix(set_perm_obj.set_n_per_perm, out['p_enrich'], method='enrichment')
    out['BHfdr_enrich'] = p_adjust_bh(out['p_enrich'])
    out['p_deplete'] = set_perm_obj.p_depletion
    out['fdr_deplete'] = fdr_from_p_matrix(set_perm_obj.set_n_per_perm, out['p_deplete'], method='depletion')
    out['BHfdr_deplete'] = p_adjust_bh(out['p_deplete'])
    #out_genes = candidates_per_set(test_obj, function_obj, annotation_obj)
    out = pd.merge(out, test_obj.candidates_in_functions_df, on='Id', how='outer')
    out = out.sort_values('p_enrich')
    return out


def make_nested_results_table(test_obj, function_obj, set_perm_obj, annotation_obj):
    out = function_obj.function_sets.groupby('Id', as_index=False).agg({'FunctionName': pd.Series.unique})
    out = out[out['Id'].isin(function_obj.function_array2d_ids)]
    out['obs_n'] = test_obj.n_candidate_per_function
    out['perm_mean_n'] = set_perm_obj.mean_per_set
    sem=set_perm_obj.sd_per_set / np.sqrt(set_perm_obj.n_permutations)
    out['perm_sem'] = sem
    e_array = np.asarray(out['obs_n'] / out['perm_mean_n'].values)
    sem_array = e_array * np.sqrt(np.square(2*sem/set_perm_obj.mean_per_set))
    log_e = np.log2(e_array, out=np.empty((np.shape(e_array)[0],)) * np.nan, where=(e_array!=0))
    out['enrich(log2)'] = log_e
    #out['u_95%(CI)'] = np.log2(e_array+sem_array, out=np.empty((np.shape(sem_array)[0],)) * np.nan, where=(e_array+sem_array!=0))
    #out['l_95%(CI)'] = np.log2(e_array-sem_array, out=np.empty((np.shape(sem_array)[0],)) * np.nan, where=(e_array-sem_array!=0))
    out['p_enrich'] = set_perm_obj.p_enrichment
    out['within_fdr_enrich'] = fdr_from_p_matrix(set_perm_obj.set_n_per_perm, out['p_enrich'], method='enrichment')
    out['within_BHfdr_enrich'] = p_adjust_bh(out['p_enrich'])
    out['p_deplete'] = set_perm_obj.p_depletion
    out['within_fdr_deplete'] = fdr_from_p_matrix(set_perm_obj.set_n_per_perm, out['p_deplete'], method='depletion')
    out['within_BHfdr_deplete'] = p_adjust_bh(out['p_deplete'])
    #out_genes = candidates_per_set(test_obj, function_obj, annotation_obj)
    out = pd.merge(out, test_obj.candidates_in_functions_df, on='Id', how='outer')
    return out



def combine_nested_results_table(results_list, per_set_list, nested_names, index_by_list_size, ):
    mod_tables = [None] * len(nested_names)
    set_n_per_perm_list = [None] * len(nested_names)
    for i, size_index in enumerate(index_by_list_size):
        this_name=nested_names[size_index]
        this_table=results_list[i]
        this_table['Label']=this_name
        mod_tables[i]=this_table
        this_per_set=per_set_list[i]
        set_n_per_perm_list[i]=this_per_set.set_n_per_perm

    merged_results=pd.concat(mod_tables)
    merged_set_n_per_perm=np.concatenate(set_n_per_perm_list, axis=0)
    merged_results['all_fdr_enrich'] = fdr_from_p_matrix(merged_set_n_per_perm, merged_results['p_enrich'], method='enrichment')
    merged_results['all_BHfdr_enrich'] = p_adjust_bh(merged_results['p_enrich'])
    merged_results['all_fdr_deplete'] = fdr_from_p_matrix(merged_set_n_per_perm, merged_results['p_deplete'], method='depletion')
    merged_results['all_BHfdr_deplete'] = p_adjust_bh(merged_results['p_deplete'])
    # reorganiase col order
    merged_results=merged_results[['Label','Id','FunctionName','obs_n','perm_mean_n','enrich(log2)','p_enrich','within_fdr_enrich','within_BHfdr_enrich','all_fdr_enrich','all_BHfdr_enrich','p_deplete','within_fdr_deplete','within_BHfdr_deplete','all_fdr_deplete','all_BHfdr_deplete','Genes']]
    merged_results = merged_results.sort_values('p_enrich')
    return merged_results


def fdr_from_p_matrix(perm_n_per_set, obs_p, method='enrichment'):
    p_matrix = perm_p_matrix(perm_n_per_set, method)
    obs_p_arr = np.asarray(obs_p)
    n_perm = p_matrix.shape[0]
    fdr_p = np.empty(len(obs_p), dtype='float64')
    obs_order = np.argsort(obs_p_arr)
    p_val, p_counts = np.unique(p_matrix, return_counts=True)
    current_max_fdr = 0
    for i, p_idx in enumerate(obs_order):
        if current_max_fdr == 1:
            fdr_p[p_idx] = 1
        else:
            obs = np.size(np.where(obs_p_arr <= obs_p_arr[p_idx]))
            exp = np.sum(p_counts[np.where(p_val <= obs_p_arr[p_idx])]) / n_perm
            i_fdr = exp / obs
            if current_max_fdr <= i_fdr < 1:
                fdr_p[p_idx] = i_fdr
                current_max_fdr = i_fdr
            elif current_max_fdr > i_fdr and i_fdr < 1:
                fdr_p[p_idx] = current_max_fdr
            else:
                fdr_p[p_idx] = 1
                current_max_fdr = 1
    return fdr_p


def p_adjust_bh(p):
    """Benjamini-Hochberg p-value correction for multiple hypothesis testing."""
    p = np.asfarray(p)
    by_descend = p.argsort()[::-1]
    by_orig = by_descend.argsort()
    steps = float(len(p)) / np.arange(len(p), 0, -1)
    q = np.minimum(1, np.minimum.accumulate(steps * p[by_descend]))
    return q[by_orig]


def load_variants(variant_file):
    variants = None
    cols = ["Chromosome", "Start", "End"]
    possible_chr = ["CHR","CHROMOSOME","chr","chromosome","Chromosome"]
    variant_header= pd.read_table(variant_file, header=None, nrows=1)
    ncols=len(variant_header.columns)
    dtype_dict = {"Chromosome": str, "Start": int, "End": int}
    if(ncols < 2 or ncols > 3):
        raise ValueError("variant inputs nust have 2 or 3 columns only!")
    has_header=0
    if(variant_header.iloc[0][0] not in possible_chr):
        has_header='infer'
    try:
        variants = pd.read_table(
            variant_file,
            header=has_header,
            names=cols[:ncols],
            dtype={col: dtype_dict[col] for col in cols[:ncols]} 
        )
    except pd.errors.ParserError:
        print(f'The file: {variant_file} does not exist. Please correct and try again.')
    return pr.PyRanges(variants.drop_duplicates())

# wrapper for results table writing
def results_writer(results_df, name_str, function_name_str, user_prefix):
    if(user_prefix==""):
        outfile_name = name_str + "-" + function_name_str + ".set_perm.txt"
    else:
        outfile_name = user_prefix + "-" + name_str + "-" + function_name_str + ".set_perm.txt"
    results_df.to_csv(path_or_buf=outfile_name, sep='\t',header=True, index=False, float_format='%.8f',mode='w+')
    return None

# global functions used in class constructors/__init__

def load_annotation_table(annotation_file):
    annotation_table = pd.read_table(
        annotation_file,
        header=0,
        names=['Chromosome', "Start", "End", "Annotation"],
        dtype={"Chromosome": str, "Start": int, "End": int, "Annotation": str}
    )
    annotation_table['Idx'] = np.arange(len(annotation_table))+1
    return annotation_table


def modify_annotation_table(annotation_table, range_modification):
    annotation_table['Start'] = annotation_table['Start'] - range_modification
    annotation_table['End'] = annotation_table['End'] + range_modification
    return annotation_table


def load_function_sets(function_set_file):
    function_sets = pd.read_table(
        function_set_file,
        header=0,
        names=['Id', "Annotation", "FunctionName"],
        dtype={"Id": str, "Annotation": str, "FunctionName": str}
    )
    return function_sets


def function_sets_to_array(function_sets, min_set_size, annotation_obj):
    sets = function_sets.join(annotation_obj.annotation_table.set_index('Annotation'), on='Annotation').groupby('Id')[
        'Idx'].apply(list)
    set_array = [s for s in sets if len(s) >= min_set_size]
    set_names = [i for i, s in enumerate(sets) if len(s) >= min_set_size]
    function_array = np.sort(listnp_to_padded_nparray(set_array))
    function_array_ids = sets.index[set_names]
    return function_array, function_array_ids

# function to combine candidate gene by function lists form two or more objects
def make_combined_candidate_by_function_df(df_list):
    zip_gene_lists = list(zip(*[df['Genes'].values for df in df_list]))
    collapsed_lists=["; ".join(g_list) for g_list in  zip_gene_lists ]
    ids=df_list[0]['Id'].values
    combined = {'Id':ids, 'Genes': collapsed_lists}
    combined_df = pd.DataFrame(data=combined)
    return combined_df

# --- classes
class AnnotationSet:
    # constructor
    def __init__(self, annotation_file='', range_modification=None):
        self.annotation_file = annotation_file
        self.range_modification = range_modification
        self.annotation_table = load_annotation_table(self.annotation_file)
        if range_modification is None:
            return
        self.annotation_table = modify_annotation_table(self.annotation_table, self.range_modification)
        self.num_annotations = self.annotation_table.shape[0]


class FunctionSets:
    # constructor
    def __init__(self, function_set_file='', min_set_size=0, annotation_obj=None):
        self.function_set_file = function_set_file
        self.min_set_size = min_set_size
        self.n_genes_with_variant = None
        self.function_sets = load_function_sets(self.function_set_file)
        self.function_array2d, self.function_array2d_ids = function_sets_to_array(self.function_sets,
        self.min_set_size,
        annotation_obj)
        self.n_per_set = np.asarray([np.size(np.where(function_array != 0)) for function_array in self.function_array2d], dtype='uint16')

    def update_from_gene_list(self, gene_list=None, annotation_obj=None):
        self.function_sets = self.function_sets[self.function_sets['Annotation'].isin(gene_list)]
        self.function_array2d, self.function_array2d_ids = function_sets_to_array(self.function_sets,
        self.min_set_size,
        annotation_obj)
        self.n_per_set = np.asarray([np.size(np.where(function_array != 0)) for function_array in self.function_array2d], dtype='uint16')
    @classmethod
    def background_refined(cls, base_fs_obj, annotation_obj, variant_obj):
        obj = cls.__new__(cls)
        obj.function_set_file = base_fs_obj.function_set_file
        obj.min_set_size = base_fs_obj.min_set_size
        obj.n_genes_with_variant = np.size(np.unique(variant_obj.annotated_variants['Idx'].values))
        obj.function_sets = base_fs_obj.function_sets
        return obj



class Variants:
    # constructor
    def __init__(self, variant_file=''):
        self.variant_file = variant_file
        self.variants = load_variants(self.variant_file)
        self.num_variants = self.variants.df.shape[0]
        self.annotated_variants = None

    def annotate_variants(self, annotation_obj):
        self.annotated_variants = self.variants.join(pr.PyRanges(annotation_obj.annotation_table)).df
        self.annotated_variants['Id'] = self.annotated_variants.Chromosome.astype(str).str.cat(
            self.annotated_variants.Start.astype(str), sep='_')

    def is_subset_of(self, other):
        #return pd.merge(self.variants, other.variants).equals(self.variants)
        return self.variants.df.shape[0]==self.variants.intersect(other.variants).df.shape[0]

    def annotation_with_variant(self):
        return self.annotated_variants['Annotation'].unique()


def multicore_make_id_idx_map_list(annotated_variants, n_cores):
    split_annotated_variant_tables = np.array_split(annotated_variants, n_cores)
    with cf.ProcessPoolExecutor(max_workers=n_cores) as executor:
        results = executor.map(make_id_idx_map_list, split_annotated_variant_tables)
    results = list(results)
    flat_results = [item for sublist in results for item in sublist]
    return flat_results


def make_id_idx_map_list(annotated_variants):  # should make this a multiprocess function!
    map_list = annotated_variants.groupby('Id')['Idx'].apply(list).tolist()
    return map_list


def get_idx_array(annotated_variants):
    """returns array with shape, enables compatibility with permutation_fset_intersect"""
    tmp_idx_array = np.asarray(np.unique(annotated_variants['Idx']))
    idx_array = np.ndarray((1, np.size(tmp_idx_array)), dtype='uint16')
    idx_array[0] = tmp_idx_array
    return idx_array.astype('uint16')


def n_candidates_per_set(annotation_obj, function_obj):
    candidate_set = set(annotation_obj.annotation_table['Annotation'].values)
    candidates_in_function_sets = function_obj.function_sets.groupby('Id')['Annotation'].apply(
        lambda x: np.unique(list(set(x).intersection(candidate_set))))
    candidates_in_function_sets = pd.DataFrame(candidates_in_function_sets[pd.Index(function_obj.function_array2d_ids)])
    candidates_in_function_sets = candidates_in_function_sets.reset_index(level=['Id'])
    candidates_in_function_sets.columns = ['Id', 'CandidateAnnotations']
    candidates_in_function_sets['n_CandidatesInSet'] = candidates_in_function_sets['CandidateAnnotations'].apply(
        lambda x: len(x))
    return candidates_in_function_sets

def candidates_per_set(candidate_array, function_obj, annotation_obj):
    candidate_idx_in_function_set = [np.intersect1d(i, candidate_array) if len(np.intersect1d(i, candidate_array)) > 0 else np.asarray(-9) for i in function_obj.function_array2d] 
    candidate_genes_in_function_set = [np.sort(annotation_obj.annotation_table.loc[annotation_obj.annotation_table['Idx'].isin(candidate_idxs)]['Annotation'].values) if np.all(candidate_idxs!=-9)  else  np.asarray(None) for candidate_idxs in candidate_idx_in_function_set]
    candidate_genes_collapsed = [np.array2string(f_set,separator=", ", max_line_width=10e6, threshold=10e6)  for f_set in candidate_genes_in_function_set]
    d = {'Id': function_obj.function_array2d_ids, 'Genes': candidate_genes_collapsed}
    df = pd.DataFrame(data=d)
    return df


class TestObject:
    # constructor
    def __init__(self, candidate_obj, background_obj, function_set_obj, annotation_obj, n_cores=1):
        if not candidate_obj.is_subset_of(background_obj):
            print("error: candidate set is not a subset of the background")
            return
        self.background_id_idx_map = multicore_make_id_idx_map_list(background_obj.annotated_variants, n_cores)
        self.candidate_array = get_idx_array(candidate_obj.annotated_variants)
        self.n_candidates = np.size(self.candidate_array)
        self.n_candidate_per_function = permutation_fset_intersect(
            (self.candidate_array, function_set_obj.function_array2d))
        self.candidates_in_functions_df = candidates_per_set(self.candidate_array, function_set_obj, annotation_obj)
    @classmethod
    def add_objects(cls, *args):
        obj = cls.__new__(cls)
        obj.background_id_idx_map = None
        obj.candidate_array = [ obj.candidate_array[0] for obj in args ]
        obj.n_candidates = sum([ obj.n_candidates for obj in args])
        obj.n_candidate_per_function = sum([ obj.n_candidate_per_function for obj in args])
        obj.candidates_in_functions_df = make_combined_candidate_by_function_df([obj.candidates_in_functions_df for obj in args])
        return obj
    @classmethod
    def nested_test(cls, cand_obj, function_set_obj, annotation_obj):
        obj = cls.__new__(cls)
        obj.background_id_idx_map = None
        obj.candidate_array = get_idx_array(cand_obj.annotated_variants)
        obj.n_candidates = np.size(obj.candidate_array)
        obj.n_candidate_per_function = permutation_fset_intersect(
            (obj.candidate_array, function_set_obj.function_array2d))
        obj.candidates_in_functions_df = candidates_per_set(obj.candidate_array, function_set_obj, annotation_obj)
        return obj
    @classmethod
    def union_of_objects(cls, a_obj, b_obj):
        obj = cls.__new__(cls)
        obj.candidate_file = [a_obj.candidate_file, b_obj.candidate_file]
        obj.background_file = [a_obj.background_file, b_obj.background_file]
        return obj


class Permutation:
    # constructor
    def __init__(self, test_obj, n_permutations, n_cores):
        self.n_permutations = n_permutations
        self.permutations = multicore_resample(test_obj.n_candidates, self.n_permutations, n_cores, test_obj.background_id_idx_map)
    @classmethod
    def nested_perm(cls, perm_obj, n_genes):
        """Return a new Permutation object, taking the first n_genes
        from each permutation"""
        obj = cls.__new__(cls)
        obj.n_permutations = perm_obj.n_permutations
        obj.permutations = perm_obj.permutations[:,:n_genes]
        return obj



class SetPerPerm:
    # constructor
    def __init__(self, permutation_obj, function_set_obj, test_obj, n_cores):
        self.set_n_per_perm = multicore_intersect(permutation_obj.permutations, function_set_obj.function_array2d, n_cores)
        self.mean_per_set = np.array(np.mean(self.set_n_per_perm, axis=0))
        self.sd_per_set = np.array(np.std(self.set_n_per_perm, axis=0))
        # var is additive. helps with joins....
        self.var_per_set = np.array(np.std(self.set_n_per_perm, axis=0))
        self.p_enrichment, self.p_depletion = calculate_p_values(test_obj.n_candidate_per_function, self.set_n_per_perm)
        self.n_candidate_per_function = test_obj.n_candidate_per_function
        self.n_permutations = permutation_obj.n_permutations
    @classmethod
    def join_objects(cls, *args):
        """Return a new SetPerPerm object, equivalent to a + b.
        Used because addition is too complex for default __init__"""
        obj = cls.__new__(cls)
        # objects should have same number of permutations!
        n_perm_list= [ obj.n_permutations for obj in args ]
        if(n_perm_list.count(n_perm_list[0]) == len(n_perm_list)):
            obj.n_permutations=n_perm_list[0]
            obj.set_n_per_perm = sum([ obj.set_n_per_perm for obj in args])
            obj.mean_per_set = sum([ obj.mean_per_set for obj in args])
            obj.var_per_set = sum([ obj.var_per_set for obj in args])
            obj.sd_per_set = np.sqrt(obj.var_per_set)
            obj.n_candidate_per_function = sum([ obj.n_candidate_per_function for obj in args])
            obj.p_enrichment, obj.p_depletion = calculate_p_values(obj.n_candidate_per_function, obj.set_n_per_perm)
            return obj
        else:
            raise ValueError("Objects must have the same number of permutations!")

# --- redundant and/or not used anymore

def perm_p_matrix(perm_n_per_set, method='enrichment'):
    if(len(perm_n_per_set.shape)>1):
        n_perms, n_sets = perm_n_per_set.shape
    else:
        n_perms = perm_n_per_set.size
        n_sets = 1
    out = np.ndarray((n_perms, n_sets), dtype='float64')
    method_int = 1
    if method == 'enrichment':
        method_int = -1
    if(len(perm_n_per_set.shape)>1):
        for i in range(n_sets):
            out[:, i] = rankdata(method_int * perm_n_per_set[:, i], method='max') /n_perms
    else:
        out=rankdata(method_int * perm_n_per_set, method='max')/n_perms
    return out


def array_of_resamples(feature_list, n_total, n_reps):
    out = np.ndarray((n_reps, n_total), dtype='uint16')
    for i in range(n_reps):
        out[i] = sample_from_feature_list(feature_list, n_total, 1.4)
    return out


def random_check_intersection(n_per_set, perms, sets, check_n):
    check_idxs = []
    n_perms = np.shape(perms)[0]
    n_sets = np.shape(sets)[0]
    for i in range(check_n):
        j = sample(range(0, n_perms - 1), 1)[0]
        k = sample(range(0, n_sets - 1), 1)[0]
        check_idxs.append(len(set(perms[j]).intersection(set(sets[k]))) == n_per_set[j][k])
    return check_idxs


# scratch
def contiguous_feature_coordinates(feature_table):
    out_df = pd.DataFrame({'Chromosome': [], 'Start': [], 'End': [], 'idx': []})
    for c in feature_table['Chromosome'].unique():
        sub_starts = feature_table[feature_table['Chromosome'] == c]['Start'].values
        sub_ends = feature_table[feature_table['Chromosome'] == c]['End'].values
        sub_lengths = sub_ends - sub_starts
        for i in range(len(sub_starts)):
            if i == 0:
                sub_starts = sub_starts - sub_starts[i] + 1
                sub_ends[i] = sub_starts[i] + sub_lengths[i]
            elif i > 0:
                sub_starts[i] = sub_ends[i - 1] + 1
                sub_ends[i] = sub_starts[i] + sub_lengths[i]
        c_df = pd.DataFrame(
            zip(repeat(c), sub_starts, sub_ends, feature_table[feature_table['Chromosome'] == c]['idx'].values),
            columns=['Chromosome', 'Start', "End", "idx"])
        out_df = pd.concat([out_df, c_df])
    return out_df
