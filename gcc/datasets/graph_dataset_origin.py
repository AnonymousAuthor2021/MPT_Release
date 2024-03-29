#!/usr/bin/env python
# encoding: utf-8
# File Name: graph_dataset.py
# Author: Jiezhong Qiu
# Create Time: 2019/12/11 12:17
# TODO:

import math
import operator

import dgl
import dgl.data
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import torch
from dgl.data import AmazonCoBuy, Coauthor
from dgl.nodeflow import NodeFlow

from gcc.datasets import data_util
import os

def worker_init_fn(worker_id):
    worker_info = torch.utils.data.get_worker_info()
    dataset = worker_info.dataset
    dataset.graphs, _ = dgl.data.utils.load_graphs(
        dataset.dgl_graphs_file, dataset.jobs[worker_id]
    )
    dataset.length = sum([g.number_of_nodes() for g in dataset.graphs])
    np.random.seed(worker_info.seed % (2 ** 32))


class LoadBalanceGraphDataset(torch.utils.data.IterableDataset):
    def __init__(self, rw_hops=64, restart_prob=0.8,
        positional_embedding_size=32, step_dist=[1.0, 0.0, 0.0],
        num_workers=1,
        dgl_graphs_file="./data_bin/dgl/motifs.bin",
        # dgl_graphs_file="./data_bin/dgl/motif_facebook.bin",
        # dgl_graphs_file="./data_bin/dgl/synthetic_motif.bin",
        num_samples=10000, num_copies=1,
        graph_transform=None, aug="rwr", num_neighbors=5, ):
        super(LoadBalanceGraphDataset).__init__()
        self.rw_hops = rw_hops
        self.num_neighbors = num_neighbors
        self.restart_prob = restart_prob
        self.positional_embedding_size = positional_embedding_size
        self.step_dist = step_dist
        self.num_samples = num_samples
        assert sum(step_dist) == 1.0
        assert positional_embedding_size > 1
        self.dgl_graphs_file = dgl_graphs_file
        print(dgl.data.utils.load_labels(dgl_graphs_file))
        graph_sizes = dgl.data.utils.load_labels(dgl_graphs_file)[
            "graph_sizes"
        ].tolist()
        print("load graph done1")
        #print(dgl.data.utils.load_labels(dgl_graphs_file))
        # a simple greedy algorithm for load balance
        # sorted graphs w.r.t its size in decreasing order
        # for each graph, assign it to the worker with least workload
        assert num_workers % num_copies == 0
        jobs = [list() for i in range(num_workers // num_copies)]
        workloads = [0] * (num_workers // num_copies)
        graph_sizes = sorted(
            enumerate(graph_sizes), key=operator.itemgetter(1), reverse=True
        )
        for idx, size in graph_sizes:
            argmin = workloads.index(min(workloads))
            workloads[argmin] += size
            jobs[argmin].append(idx)
        self.jobs = jobs * num_copies
        self.total = self.num_samples * num_workers
        self.graph_transform = graph_transform
        assert aug in ("rwr", "ns")
        self.aug = aug

    def __len__(self):
        return self.num_samples * num_workers

    def __iter__(self):
        degrees = torch.cat([g.in_degrees().double() ** 0.75 for g in self.graphs])
        prob = degrees / torch.sum(degrees)
        samples = np.random.choice(
            self.length, size=self.num_samples, replace=True, p=prob.numpy()
        )
        for idx in samples:
            yield self.__getitem__(idx)

    def __getitem__(self, idx):
        graph_idx = 0
        node_idx = idx
        for i in range(len(self.graphs)):
            if node_idx < self.graphs[i].number_of_nodes():
                graph_idx = i
                break
            else:
                node_idx -= self.graphs[i].number_of_nodes()

        step = np.random.choice(len(self.step_dist), 1, p=self.step_dist)[0]
        if step == 0:
            other_node_idx = node_idx
        else:
            other_node_idx = dgl.contrib.sampling.random_walk(
                g=self.graphs[graph_idx], seeds=[node_idx], num_traces=1, num_hops=step
            )[0][0][-1].item()

        if self.aug == "rwr":
            max_nodes_per_seed = max(
                self.rw_hops,
                int(
                    (
                        (self.graphs[graph_idx].in_degree(node_idx) ** 0.75)
                        * math.e
                        / (math.e - 1)
                        / self.restart_prob
                    )
                    + 0.5
                ),
            )
            traces = dgl.contrib.sampling.random_walk_with_restart(
                self.graphs[graph_idx],
                seeds=[node_idx, other_node_idx],
                restart_prob=self.restart_prob,
                max_nodes_per_seed=max_nodes_per_seed,
            )
        elif self.aug == "ns":
            prob = dgl.backend.tensor([], dgl.backend.float32)
            prob = dgl.backend.zerocopy_to_dgl_ndarray(prob)
            nf1 = dgl.contrib.sampling.sampler._CAPI_NeighborSampling(
                self.graphs[graph_idx]._graph,
                dgl.utils.toindex([node_idx]).todgltensor(),
                0,  # batch_start_id
                1,  # batch_size
                1,  # workers
                self.num_neighbors,  # expand_factor
                self.rw_hops,  # num_hops
                "out",
                False,
                prob,
            )[0]
            nf1 = NodeFlow(self.graphs[graph_idx], nf1)
            trace1 = [nf1.layer_parent_nid(i) for i in range(nf1.num_layers)]
            nf2 = dgl.contrib.sampling.sampler._CAPI_NeighborSampling(
                self.graphs[graph_idx]._graph,
                dgl.utils.toindex([other_node_idx]).todgltensor(),
                0,  # batch_start_id
                1,  # batch_size
                1,  # workers
                self.num_neighbors,  # expand_factor
                self.rw_hops,  # num_hops
                "out",
                False,
                prob,
            )[0]
            nf2 = NodeFlow(self.graphs[graph_idx], nf2)
            trace2 = [nf2.layer_parent_nid(i) for i in range(nf2.num_layers)]
            traces = [trace1, trace2]

        graph_q = data_util._rwr_trace_to_dgl_graph(
            g=self.graphs[graph_idx],
            graph_idx=graph_idx,
            seed=node_idx,
            trace=traces[0],
            positional_embedding_size=self.positional_embedding_size,
        )
        #print(graph_idx, node_idx, other_node_idx) #node_idx == other_node_idx
        #exit(0)
        graph_k = data_util._rwr_trace_to_dgl_graph(
            g=self.graphs[graph_idx],
            graph_idx=graph_idx,
            seed=other_node_idx,
            trace=traces[1],
            positional_embedding_size=self.positional_embedding_size,
        )
        #print(len(graph_q.unbatch()))
        #for i in range(len(graph_q)):
        #    graph_q[i].ndata['idx'] = graph_idx
        #print("load")
        #print(graph_idx, node_idx, other_node_idx)
        #graph_q.ndata_schemes['idx'] = node_idx
        #graph_k.ndata['idx'] = node_idx
        if self.graph_transform:
            graph_q = self.graph_transform(graph_q)
            graph_k = self.graph_transform(graph_k)
        # pretrain
        # print("here", graph_q, graph_k)
        return graph_q, graph_k

class LoadBalanceGraphDataset2(torch.utils.data.IterableDataset):
    def __init__(self, rw_hops=64, restart_prob=0.8,
        positional_embedding_size=32, step_dist=[1.0, 0.0, 0.0],
        num_workers=1,
        dgl_graphs_file="./data_bin/dgl/motifs.bin",
        num_samples=10000, num_copies=1,
        graph_transform=None, aug="rwr", num_neighbors=5, ):
        super(LoadBalanceGraphDataset2).__init__()
        self.rw_hops = rw_hops
        self.num_neighbors = num_neighbors
        self.restart_prob = restart_prob
        self.positional_embedding_size = positional_embedding_size
        self.step_dist = step_dist
        self.num_samples = num_samples
        assert sum(step_dist) == 1.0
        assert positional_embedding_size > 1
        self.dgl_graphs_file = dgl_graphs_file
        print(dgl.data.utils.load_labels(dgl_graphs_file))
        graph_sizes = dgl.data.utils.load_labels(dgl_graphs_file)[
            "graph_sizes"
        ].tolist()
        print("load graph done1")
        #print(dgl.data.utils.load_labels(dgl_graphs_file))
        assert num_workers % num_copies == 0
        jobs = [list() for i in range(num_workers // num_copies)]
        workloads = [0] * (num_workers // num_copies)
        graph_sizes = sorted(
            enumerate(graph_sizes), key=operator.itemgetter(1), reverse=True
        )
        for idx, size in graph_sizes:
            argmin = workloads.index(min(workloads))
            workloads[argmin] += size
            jobs[argmin].append(idx)
        self.jobs = jobs * num_copies
        self.total = self.num_samples * num_workers
        self.graph_transform = graph_transform
        assert aug in ("rwr", "ns")
        self.aug = aug
        #self.num_classes = len(self.graphs)

    def __len__(self):
        return self.num_samples * num_workers

    def __iter__(self):
        degrees = torch.cat([g.in_degrees().double() ** 0.75 for g in self.graphs])
        prob = degrees / torch.sum(degrees)
        samples = np.random.choice(
            self.length, size=self.num_samples, replace=True, p=prob.numpy()
        )
        for idx in samples:
            yield self.__getitem__(idx)

    def __getitem__(self, idx):
        graph_idx = 0
        node_idx = idx
        for i in range(len(self.graphs)):
            if node_idx < self.graphs[i].number_of_nodes():
                graph_idx = i
                #graph_idx = nodenum2index[self.graphs[i].number_of_nodes()]
                break
            else:
                node_idx -= self.graphs[i].number_of_nodes()

        step = np.random.choice(len(self.step_dist), 1, p=self.step_dist)[0]
        if step == 0:
            other_node_idx = node_idx
        else:
            other_node_idx = dgl.contrib.sampling.random_walk(
                g=self.graphs[graph_idx], seeds=[node_idx], num_traces=1, num_hops=step
            )[0][0][-1].item()

        if self.aug == "rwr":
            max_nodes_per_seed = max(
                self.rw_hops,
                int(
                    (
                        (self.graphs[graph_idx].in_degree(node_idx) ** 0.75)
                        * math.e
                        / (math.e - 1)
                        / self.restart_prob
                    )
                    + 0.5
                ),
            )
            traces = dgl.contrib.sampling.random_walk_with_restart(
                self.graphs[graph_idx],
                seeds=[node_idx, other_node_idx],
                restart_prob=self.restart_prob,
                max_nodes_per_seed=max_nodes_per_seed,
            )
        elif self.aug == "ns":
            prob = dgl.backend.tensor([], dgl.backend.float32)
            prob = dgl.backend.zerocopy_to_dgl_ndarray(prob)
            nf1 = dgl.contrib.sampling.sampler._CAPI_NeighborSampling(
                self.graphs[graph_idx]._graph,
                dgl.utils.toindex([node_idx]).todgltensor(),
                0,  # batch_start_id
                1,  # batch_size
                1,  # workers
                self.num_neighbors,  # expand_factor
                self.rw_hops,  # num_hops
                "out",
                False,
                prob,
            )[0]
            nf1 = NodeFlow(self.graphs[graph_idx], nf1)
            trace1 = [nf1.layer_parent_nid(i) for i in range(nf1.num_layers)]
            nf2 = dgl.contrib.sampling.sampler._CAPI_NeighborSampling(
                self.graphs[graph_idx]._graph,
                dgl.utils.toindex([other_node_idx]).todgltensor(),
                0,  # batch_start_id
                1,  # batch_size
                1,  # workers
                self.num_neighbors,  # expand_factor
                self.rw_hops,  # num_hops
                "out",
                False,
                prob,
            )[0]
            nf2 = NodeFlow(self.graphs[graph_idx], nf2)
            trace2 = [nf2.layer_parent_nid(i) for i in range(nf2.num_layers)]
            traces = [trace1, trace2]

        graph_q = data_util._rwr_trace_to_dgl_graph(
            g=self.graphs[graph_idx],
            graph_idx=graph_idx,
            seed=node_idx,
            trace=traces[0],
            positional_embedding_size=self.positional_embedding_size,
        )
        #print(graph_idx, node_idx, other_node_idx) #node_idx == other_node_idx
        #exit(0)
        if self.graph_transform:
            graph_q = self.graph_transform(graph_q)
        # print("here", graph_q, graph_k)
        return graph_q, graph_idx

motif_dict = {}
graph_motif_list = {}
'''
nodenum2name = {4843953:"livejournal", 3097165:"facebook", 896305:"imdb", 540486:"dblp_netrep", 317080:"dblp_snap", 
137969:"academia", 1134890:"youtube", 81306:"twitter", 334863:"amazon", 1632803:"pokec", 875713:"google",
265009:"euall", 36692:"enron", 80513:"flickr", 37700:"git", 5000:"hindex_top", 18771:"ca-AstroPh", 23133:"ca-CondMat",
5241:"ca-GrQc", 12007:"ca-HepPh", 9875:"ca-HepTh", 2708:"cora", 3264:"citeseer", 2405:"wiki", 881:"terrorist",
1190:"usa", 131:"brazil", 399:"europe"
}
nodenum2index = {4843953:0, 3097165:1, 896305:2, 540486:3, 317080:4, 
137969:5, 1134890:6, 81306:7, 334863:8, 1632803:9, 875713:10,
265009:11, 36692:12, 80513:13, 37700:14, 5000:15, 18771:16, 23133:17,
5241:18, 12007:19, 9875:20, 2708:21, 3264:22, 2405:23, 881:24,
1190:25, 131:26, 399:27
}
'''
nodenum2index = {2708:0, 3264:1}

def load_motif3():
    #names = ["dblp_netrep", "ca-GrQc", "ca-HepPh"]
    #names1 = ["livejournal", "facebook", "imdb", "dblp_netrep", "dblp_snap", "academia", "ca-HepPh", "euall", "google", "flickr"]
    #names = ["livejournal", "dblp_netrep", "dblp_snap", "hindex_top", "ca-GrQc", "ca-HepPh", "euall"]
    names = ["cora", "citeseer"]
    
    counts = []
    for i in range(len(names)):
        count = 0
        graph_list = np.zeros(15)
        f = open("./motifs/" + names[i] + "-counts.out")
        for line in f:
            nums = [int(x) for x in line.split()]
            motif_dict[(i, count)] = nums
            count += 1
            graph_list += np.array(nums)
        graph_motif_list[i] = graph_list
        counts.append(count)
    
    for key, val in motif_dict.items():
        for k in range(len(val)):
            if graph_motif_list[key[0]][k] != 0:
                motif_dict[key][k] = val[k] * counts[key[0]] / graph_motif_list[key[0]][k]
            else:
                motif_dict[key][k] = 0.0
        if sum(motif_dict[key]) == 0.0:
            motif_dict[key] = np.array([0,0,0,0,0,0,0,0,0,0,0,0,0,0,0])
        else:
            motif_dict[key] = np.array([x*1.0/sum(motif_dict[key]) for x in motif_dict[key]])
    return motif_dict, graph_motif_list

def load_motif2():
    names = ["cora"]
    counts = []
    for i in range(len(names)):
        count = 0
        graph_list = np.zeros(15)
        f = open("./cora/" + names[i] + "_count.out")
        for line in f:
            nums = [int(x) for x in line.split()]
            motif_dict[(i, count)] = nums
            count += 1
            graph_list += np.array(nums)
        graph_motif_list[i] = graph_list
        counts.append(count)
    
    for key, val in motif_dict.items():
        for k in range(len(val)):
            if graph_motif_list[key[0]][k] != 0:
                motif_dict[key][k] = val[k] * counts[key[0]] / graph_motif_list[key[0]][k]
            else:
                motif_dict[key][k] = 0.0
        if sum(motif_dict[key]) == 0.0:
            motif_dict[key] = np.array([0,0,0,0,0,0,0,0,0,0,0,0,0,0,0])
        else:
            motif_dict[key] = np.array([x*1.0/sum(motif_dict[key]) for x in motif_dict[key]])
    return motif_dict, graph_motif_list

def load_motif():
    names = ["livejournal", "facebook", "imdb", "dblp_netrep", "dblp_snap", "academia"]
    counts = []
    for i in range(len(names)):
        count = 0
        graph_list = np.zeros(15)
        f = open("./motifs/" + names[i] + "-counts.out")
        for line in f:
            nums = [int(x) for x in line.split()]
            motif_dict[(i, count)] = nums
            count += 1
            graph_list += np.array(nums)
        graph_motif_list[i] = graph_list
        counts.append(count)
    
    for key, val in motif_dict.items():
        for k in range(len(val)):
            if graph_motif_list[key[0]][k] != 0:
                motif_dict[key][k] = val[k] * counts[key[0]] / graph_motif_list[key[0]][k]
            else:
                motif_dict[key][k] = 0.0
        if sum(motif_dict[key]) == 0.0:
            motif_dict[key] = np.array([0,0,0,0,0,0,0,0,0,0,0,0,0,0,0])
        else:
            motif_dict[key] = np.array([x*1.0/sum(motif_dict[key]) for x in motif_dict[key]])
    return motif_dict, graph_motif_list
    
class LoadBalanceGraphDataset3(torch.utils.data.IterableDataset):
    def __init__(self, rw_hops=64, restart_prob=0.8,
        positional_embedding_size=32, step_dist=[1.0, 0.0, 0.0],
        num_workers=1,
        #dgl_graphs_file="./data_bin/dgl/small.bin",
        dgl_graphs_file="./data_bin/dgl/all.bin",
        #dgl_graphs_file="./data_bin/dgl/cora.bin",
        num_samples=10000, num_copies=1,
        graph_transform=None, aug="rwr", num_neighbors=5, ):
        super(LoadBalanceGraphDataset3).__init__()
        self.rw_hops = rw_hops
        self.num_neighbors = num_neighbors
        self.restart_prob = restart_prob
        self.positional_embedding_size = positional_embedding_size
        self.step_dist = step_dist
        self.num_samples = num_samples
        assert sum(step_dist) == 1.0
        assert positional_embedding_size > 1
        self.dgl_graphs_file = dgl_graphs_file
        print(dgl.data.utils.load_labels(dgl_graphs_file))
        graph_sizes = dgl.data.utils.load_labels(dgl_graphs_file)[
            "graph_sizes"
        ].tolist()
        print(dgl_graphs_file, "load graph done1")
        #print(dgl.data.utils.load_labels(dgl_graphs_file))
        assert num_workers % num_copies == 0
        jobs = [list() for i in range(num_workers // num_copies)]
        workloads = [0] * (num_workers // num_copies)
        graph_sizes = sorted(
            enumerate(graph_sizes), key=operator.itemgetter(1), reverse=True
        )
        for idx, size in graph_sizes:
            argmin = workloads.index(min(workloads))
            workloads[argmin] += size
            jobs[argmin].append(idx)
        self.jobs = jobs * num_copies
        self.total = self.num_samples * num_workers
        self.graph_transform = graph_transform
        assert aug in ("rwr", "ns")
        self.aug = aug
        self.motif_dict, self.graph_motif_list = load_motif3() 
        #self.motif_dict, self.graph_motif_list = load_motif() 
        #self.motif_dict, self.graph_motif_list = load_motif2() 

    def __len__(self):
        return self.num_samples * num_workers

    def __iter__(self):
        degrees = torch.cat([g.in_degrees().double() ** 0.75 for g in self.graphs])
        prob = degrees / torch.sum(degrees)
        samples = np.random.choice(
            self.length, size=self.num_samples, replace=True, p=prob.numpy()
        )
        for idx in samples:
            yield self.__getitem__(idx)

    def __getitem__(self, idx):
        graph_idx = 0
        node_idx = idx
        for i in range(len(self.graphs)):
            if node_idx < self.graphs[i].number_of_nodes():
                #graph_idx = i
                graph_idx = nodenum2index[self.graphs[i].number_of_nodes()]
                break
            else:
                node_idx -= self.graphs[i].number_of_nodes()
        
        step = np.random.choice(len(self.step_dist), 1, p=self.step_dist)[0]
        if step == 0:
            other_node_idx = node_idx
        else:
            other_node_idx = dgl.contrib.sampling.random_walk(
                g=self.graphs[graph_idx], seeds=[node_idx], num_traces=1, num_hops=step
            )[0][0][-1].item()

        if self.aug == "rwr":
            max_nodes_per_seed = max(
                self.rw_hops,
                int(
                    (
                        (self.graphs[graph_idx].in_degree(node_idx) ** 0.75)
                        * math.e
                        / (math.e - 1)
                        / self.restart_prob
                    )
                    + 0.5
                ),
            )
            traces = dgl.contrib.sampling.random_walk_with_restart(
                self.graphs[graph_idx],
                seeds=[node_idx, other_node_idx],
                restart_prob=self.restart_prob,
                max_nodes_per_seed=max_nodes_per_seed,
            )
        elif self.aug == "ns":
            prob = dgl.backend.tensor([], dgl.backend.float32)
            prob = dgl.backend.zerocopy_to_dgl_ndarray(prob)
            nf1 = dgl.contrib.sampling.sampler._CAPI_NeighborSampling(
                self.graphs[graph_idx]._graph,
                dgl.utils.toindex([node_idx]).todgltensor(),
                0,  # batch_start_id
                1,  # batch_size
                1,  # workers
                self.num_neighbors,  # expand_factor
                self.rw_hops,  # num_hops
                "out",
                False,
                prob,
            )[0]
            nf1 = NodeFlow(self.graphs[graph_idx], nf1)
            trace1 = [nf1.layer_parent_nid(i) for i in range(nf1.num_layers)]
            nf2 = dgl.contrib.sampling.sampler._CAPI_NeighborSampling(
                self.graphs[graph_idx]._graph,
                dgl.utils.toindex([other_node_idx]).todgltensor(),
                0,  # batch_start_id
                1,  # batch_size
                1,  # workers
                self.num_neighbors,  # expand_factor
                self.rw_hops,  # num_hops
                "out",
                False,
                prob,
            )[0]
            nf2 = NodeFlow(self.graphs[graph_idx], nf2)
            trace2 = [nf2.layer_parent_nid(i) for i in range(nf2.num_layers)]
            traces = [trace1, trace2]

        graph_q = data_util._rwr_trace_to_dgl_graph(
            g=self.graphs[graph_idx],
            graph_idx=graph_idx,
            seed=node_idx,
            trace=traces[0],
            positional_embedding_size=self.positional_embedding_size,
        )
        #print(graph_idx, node_idx, other_node_idx) #node_idx == other_node_idx
        #exit(0)
        if self.graph_transform:
            graph_q = self.graph_transform(graph_q)
        # print("here", graph_q, graph_k)
        if (graph_idx, node_idx) in self.motif_dict:
            return graph_q, self.motif_dict[(graph_idx, node_idx)]
        else:
            print(graph_idx, node_idx)
            print(self.graphs)
            #exit(0)
            return

class GraphDataset(torch.utils.data.Dataset):
    def __init__(
        self,
        rw_hops=64,
        subgraph_size=64,
        restart_prob=0.8,
        positional_embedding_size=32,
        step_dist=[1.0, 0.0, 0.0],
    ):
        super(GraphDataset).__init__()
        self.rw_hops = rw_hops
        self.subgraph_size = subgraph_size
        self.restart_prob = restart_prob
        self.positional_embedding_size = positional_embedding_size
        self.step_dist = step_dist
        assert sum(step_dist) == 1.0
        assert positional_embedding_size > 1
        #  graphs = []
        graphs, _ = dgl.data.utils.load_graphs(
            "data_bin/dgl/lscc_graphs.bin", [0, 1, 2]
        )
        for name in ["cs", "physics"]:
            g = Coauthor(name)[0]
            g.remove_nodes((g.in_degrees() == 0).nonzero().squeeze())
            g.readonly()
            graphs.append(g)
        for name in ["computers", "photo"]:
            g = AmazonCoBuy(name)[0]
            g.remove_nodes((g.in_degrees() == 0).nonzero().squeeze())
            g.readonly()
            graphs.append(g)
        # more graphs are comming ...
        print("load graph done")
        self.graphs = graphs
        self.length = sum([g.number_of_nodes() for g in self.graphs])

    def __len__(self):
        return self.length

    def _convert_idx(self, idx):
        graph_idx = 0
        node_idx = idx
        for i in range(len(self.graphs)):
            if node_idx < self.graphs[i].number_of_nodes():
                graph_idx = i
                break
            else:
                node_idx -= self.graphs[i].number_of_nodes()
        return graph_idx, node_idx

    def __getitem__(self, idx):
        graph_idx, node_idx = self._convert_idx(idx)

        step = np.random.choice(len(self.step_dist), 1, p=self.step_dist)[0]
        if step == 0:
            other_node_idx = node_idx
        else:
            other_node_idx = dgl.contrib.sampling.random_walk(
                g=self.graphs[graph_idx], seeds=[node_idx], num_traces=1, num_hops=step
            )[0][0][-1].item()

        max_nodes_per_seed = max(
            self.rw_hops,
            int(
                (
                    self.graphs[graph_idx].out_degree(node_idx)
                    * math.e
                    / (math.e - 1)
                    / self.restart_prob
                )
                + 0.5
            ),
        )
        traces = dgl.contrib.sampling.random_walk_with_restart(
            self.graphs[graph_idx],
            seeds=[node_idx, other_node_idx],
            restart_prob=self.restart_prob,
            max_nodes_per_seed=max_nodes_per_seed,
        )

        graph_q = data_util._rwr_trace_to_dgl_graph(
            g=self.graphs[graph_idx],
            graph_idx = graph_idx,
            seed=node_idx,
            trace=traces[0],
            positional_embedding_size=self.positional_embedding_size,
            entire_graph=hasattr(self, "entire_graph") and self.entire_graph,
        )
        graph_k = data_util._rwr_trace_to_dgl_graph(
            g=self.graphs[graph_idx],
            graph_idx = graph_idx,
            seed=other_node_idx,
            trace=traces[1],
            positional_embedding_size=self.positional_embedding_size,
            entire_graph=hasattr(self, "entire_graph") and self.entire_graph,
        )
        #print("graph dataset here")
        return graph_q, graph_k


class NodeClassificationDataset(GraphDataset):
    def __init__(
        self,
        dataset,
        rw_hops=64,
        subgraph_size=64,
        restart_prob=0.8,
        positional_embedding_size=32,
        step_dist=[1.0, 0.0, 0.0],
    ):
        self.rw_hops = rw_hops
        self.subgraph_size = subgraph_size
        self.restart_prob = restart_prob
        self.positional_embedding_size = positional_embedding_size
        self.step_dist = step_dist
        assert positional_embedding_size > 1

        if dataset == "motif":
            self.graphs = self._create_dgl_graph2()
        else:
            self.data = data_util.create_node_classification_dataset(dataset).data
            print(self.data)
            self.graphs = [self._create_dgl_graph(self.data)]
        print(self.graphs)
        #exit(0)
        self.length = sum([g.number_of_nodes() for g in self.graphs])
        self.total = self.length

    def _create_dgl_graph(self, data):
        graph = dgl.DGLGraph()
        src, dst = data.edge_index.tolist()
        num_nodes = data.edge_index.max() + 1
        graph.add_nodes(num_nodes)
        graph.add_edges(src, dst)
        graph.add_edges(dst, src)
        graph.readonly()
        return graph
    
    def _create_dgl_graph2(self):
        graphs = []
        path = "./data/struc2vec/motifs/"
        for file1 in os.listdir(path):
            print(file1)
            nxg = nx.read_edgelist(path + file1)
            graph = dgl.DGLGraph(nxg)   
            graph.readonly()
            graphs.append(graph)
        return graphs

class GraphClassificationDataset(NodeClassificationDataset):
    def __init__(
        self,
        dataset,
        rw_hops=64,
        subgraph_size=64,
        restart_prob=0.8,
        positional_embedding_size=32,
        step_dist=[1.0, 0.0, 0.0],
    ):
        self.rw_hops = rw_hops
        self.subgraph_size = subgraph_size
        self.restart_prob = restart_prob
        self.positional_embedding_size = positional_embedding_size
        self.step_dist = step_dist
        self.entire_graph = True
        assert positional_embedding_size > 1

        self.dataset = data_util.create_graph_classification_dataset(dataset)
        self.graphs = self.dataset.graph_lists

        self.length = len(self.graphs)
        self.total = self.length

    def _convert_idx(self, idx):
        graph_idx = idx
        node_idx = self.graphs[idx].out_degrees().argmax().item()
        return graph_idx, node_idx


class GraphClassificationDatasetLabeled(GraphClassificationDataset):
    def __init__(
        self,
        dataset,
        rw_hops=64,
        subgraph_size=64,
        restart_prob=0.8,
        positional_embedding_size=32,
        step_dist=[1.0, 0.0, 0.0],
    ):
        super(GraphClassificationDatasetLabeled, self).__init__(
            dataset,
            rw_hops,
            subgraph_size,
            restart_prob,
            positional_embedding_size,
            step_dist,
        )
        #print(self.data.y)
        #exit(0)
        self.num_classes = self.dataset.num_labels
        self.entire_graph = True
        self.dict = [self.getitem(idx) for idx in range(len(self))]

    def __getitem__(self, idx):
        return self.dict[idx]

    def getitem(self, idx):
        graph_idx = idx
        node_idx = self.graphs[idx].out_degrees().argmax().item()

        traces = dgl.contrib.sampling.random_walk_with_restart(
            self.graphs[graph_idx],
            seeds=[node_idx],
            restart_prob=self.restart_prob,
            max_nodes_per_seed=self.rw_hops,
        )

        graph_q = data_util._rwr_trace_to_dgl_graph(
            g=self.graphs[graph_idx],
            graph_idx = graph_idx,
            seed=node_idx,
            trace=traces[0],
            positional_embedding_size=self.positional_embedding_size,
            entire_graph=True,
        )
        return graph_q, self.dataset.graph_labels[graph_idx].item()


class NodeClassificationDatasetLabeled(NodeClassificationDataset):
    def __init__(
        self, dataset, rw_hops=64,
        subgraph_size=64, restart_prob=0.8,
        positional_embedding_size=32, step_dist=[1.0, 0.0, 0.0],
        cat_prone=False,
    ):
        super(NodeClassificationDatasetLabeled, self).__init__(
            dataset, rw_hops,
            subgraph_size, restart_prob,
            positional_embedding_size, step_dist,
        )
        assert len(self.graphs) == 1
        self.num_classes = self.data.y.shape[1]

    def __getitem__(self, idx):
        graph_idx = 0
        node_idx = idx
        for i in range(len(self.graphs)):
            if node_idx < self.graphs[i].number_of_nodes():
                graph_idx = i
                break
            else:
                node_idx -= self.graphs[i].number_of_nodes()

        traces = dgl.contrib.sampling.random_walk_with_restart(
            self.graphs[graph_idx],
            seeds=[node_idx],
            restart_prob=self.restart_prob,
            max_nodes_per_seed=self.rw_hops,
        )

        graph_q = data_util._rwr_trace_to_dgl_graph(
            g=self.graphs[graph_idx],
            graph_idx = graph_idx,
            seed=node_idx,
            trace=traces[0],
            positional_embedding_size=self.positional_embedding_size,
        )
        #with label finetune 
        #print("here node")
        #print(graph_q, self.data.y[idx].argmax().item())
        #exit(0)
        return graph_q, self.data.y[idx].argmax().item()


class NodeClassificationDatasetLabeled2(NodeClassificationDataset):
    def __init__(
        self, dataset, rw_hops=64,
        subgraph_size=64, restart_prob=0.8,
        positional_embedding_size=32, step_dist=[1.0, 0.0, 0.0],
        cat_prone=False,
    ):
        super(NodeClassificationDatasetLabeled2, self).__init__(
            dataset, rw_hops,
            subgraph_size, restart_prob,
            positional_embedding_size, step_dist,
        )
        # assert len(self.graphs) == 1
        self.num_classes = len(self.graphs)

    def __getitem__(self, idx):
        graph_idx = 0
        node_idx = idx
        for i in range(len(self.graphs)):
            if node_idx < self.graphs[i].number_of_nodes():
                graph_idx = i
                break
            else:
                node_idx -= self.graphs[i].number_of_nodes()

        traces = dgl.contrib.sampling.random_walk_with_restart(
            self.graphs[graph_idx],
            seeds=[node_idx],
            restart_prob=self.restart_prob,
            max_nodes_per_seed=self.rw_hops,
        )

        graph_q = data_util._rwr_trace_to_dgl_graph(
            g=self.graphs[graph_idx],
            graph_idx = graph_idx,
            seed=node_idx,
            trace=traces[0],
            positional_embedding_size=self.positional_embedding_size,
        )
        #with label finetune 
        #print(graph_q, graph_idx)
        return graph_q, graph_idx


if __name__ == "__main__":
    num_workers = 1
    import psutil

    mem = psutil.virtual_memory()
    print(mem.used / 1024 ** 3)
    graph_dataset = LoadBalanceGraphDataset(
        num_workers=num_workers, aug="ns", rw_hops=4, num_neighbors=5
    )
    mem = psutil.virtual_memory()
    print(mem.used / 1024 ** 3)
    graph_loader = torch.utils.data.DataLoader(
        graph_dataset,
        batch_size=1,
        collate_fn=data_util.batcher(),
        num_workers=num_workers,
        worker_init_fn=worker_init_fn,
    )
    mem = psutil.virtual_memory()
    print(mem.used / 1024 ** 3)
    for step, batch in enumerate(graph_loader):
        print("bs", batch[0].batch_size)
        print("n=", batch[0].number_of_nodes())
        print("m=", batch[0].number_of_edges())
        mem = psutil.virtual_memory()
        print(mem.used / 1024 ** 3)
        #  print(batch.graph_q)
        #  print(batch.graph_q.ndata['pos_directed'])
        print(batch[0].ndata["pos_undirected"])
    #exit(0)
    graph_dataset = NodeClassificationDataset(dataset="wikipedia")
    graph_loader = torch.utils.data.DataLoader(
        dataset=graph_dataset,
        batch_size=20,
        collate_fn=data_util.batcher(),
        shuffle=True,
        num_workers=4,
    )
    for step, batch in enumerate(graph_loader):
        print(batch.graph_q)
        print(batch.graph_q.ndata["x"].shape)
        print(batch.graph_q.batch_size)
        print("max", batch.graph_q.edata["efeat"].max())
        break
