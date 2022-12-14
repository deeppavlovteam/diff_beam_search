from __future__ import print_function

import re

import torch
import torch.nn as nn
import torch.nn.utils
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_packed_sequence, pack_padded_sequence

from nltk.translate.bleu_score import corpus_bleu, sentence_bleu, SmoothingFunction
import time
import numpy as np
import argparse, os, sys
from tqdm import tqdm

from logger import Logger
from util import read_corpus, data_iter, batch_slice, infer_mask, gpu_mem_dump
from vocab import Vocab, VocabEntry
import math
from expected_bleu.modules.expectedMultiBleu import bleu, bleu_with_bp
from expected_bleu.TF_GOOGLE_NMT import compute_bleu
from expected_bleu.modules.utils import bleu_score

def init_config():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', default=5783287, type=int, help='random seed')
    parser.add_argument('--cuda', action='store_true', default=False, help='use gpu')
    parser.add_argument('--mode', choices=['train', 'raml_train', 'test', 'sample', 'train_sampling',
                        'prob', 'interactive', 'custom', 'custom2', 'custom3', 'train_actor', 'relax_train'], default='train', help='run mode')
    parser.add_argument('--vocab', type=str, help='path of the serialized vocabulary')
    parser.add_argument('--batch_size', default=32, type=int, help='batch size')
    parser.add_argument('--beam_size', default=5, type=int, help='beam size for beam search')
    parser.add_argument('--sample_size', default=10, type=int, help='sample size')
    parser.add_argument('--embed_size', default=256, type=int, help='size of word embeddings')
    parser.add_argument('--hidden_size', default=256, type=int, help='size of LSTM hidden states')
    parser.add_argument('--dropout', default=0., type=float, help='dropout rate')

    parser.add_argument('--train_src', type=str, help='path to the training source file')
    parser.add_argument('--train_tgt', type=str, help='path to the training target file')
    parser.add_argument('--dev_src', type=str, help='path to the dev source file')
    parser.add_argument('--dev_tgt', type=str, help='path to the dev target file')
    parser.add_argument('--test_src', type=str, help='path to the test source file')
    parser.add_argument('--test_tgt', type=str, help='path to the test target file')

    parser.add_argument('--decode_max_time_step', default=200, type=int, help='maximum number of time steps used '
                                                                              'in decoding and sampling')
    parser.add_argument('--valid_niter', default=800, type=int, help='every n iterations to perform validation')
    parser.add_argument('--valid_metric', default='bleu', choices=['bleu', 'ppl', 'word_acc', 'sent_acc'], help='metric used for validation')
    parser.add_argument('--log_every', default=400, type=int, help='every n iterations to log training statistics')
    parser.add_argument('--load_model', default=None, type=str, help='load a pre-trained model')
    parser.add_argument('--save_to', default='model', type=str, help='save trained model to')
    parser.add_argument('--save_model_after', default=0, type=int, help='save the model only after n validation iterations')
    parser.add_argument('--save_to_file', default=None, type=str, help='if provided, save decoding results to file')
    parser.add_argument('--save_nbest', default=False, action='store_true', help='save nbest decoding results')
    parser.add_argument('--patience', default=5, type=int, help='training patience')
    parser.add_argument('--uniform_init', default=None, type=float, help='if specified, use uniform initialization for all parameters')
    parser.add_argument('--clip_grad', default=5., type=float, help='clip gradients')
    parser.add_argument('--max_niter', default=-1, type=int, help='maximum number of training iterations')
    parser.add_argument('--max_epoch', default=10, type=int, help='maximum number of training iterations')
    parser.add_argument('--lr', default=0.001, type=float, help='learning rate')
    parser.add_argument('--lr_warm', default=0.0001, type=float, help='learning rate for warmed model')
    parser.add_argument('--lr_decay', default=0.5, type=float, help='decay learning rate if the validation performance drops')

    # raml training
    parser.add_argument('--debug', default=False, action='store_true')
    parser.add_argument('--temp', default=0.85, type=float, help='temperature in reward distribution')
    parser.add_argument('--raml_sample_mode', default='pre_sample',
                        choices=['pre_sample', 'hamming_distance', 'hamming_distance_impt_sample'],
                        help='sample mode when using RAML')
    parser.add_argument('--raml_sample_file', type=str, help='path to the sampled targets')
    parser.add_argument('--raml_bias_groundtruth', action='store_true', default=False, help='make sure ground truth y* is in samples')

    parser.add_argument('--smooth_bleu', action='store_true', default=False,
                        help='smooth sentence level BLEU score.')
    parser.add_argument('--sentence_bleu', action='store_true', default=False,
                        help='use sentence level for bleu calculation.')

    #TODO: greedy sampling is still buggy!
    parser.add_argument('--sample_method', default='random', choices=['random', 'greedy','gumbel'])
    parser.add_argument('--use_teacher_forcing',action='store_true', default=False)
    parser.add_argument('--st_gumbel',action='store_true', default=False, help='use gumbel Straight-Through version')
    parser.add_argument('--cross_entropy',action='store_true', default=False, help='use cross-entropy as a loss function')

    args = parser.parse_args()

    # seed the RNG
    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)
    np.random.seed(args.seed * 13 // 7)

    return args


def input_transpose(sents, pad_token, max_seq_len, batches_num=10):
    max_len = max(len(s) for s in sents)
    batch_size = len(sents)
    sents_t = []
    for i in range(max_len):
        sents_t.append([sents[k][i] if len(sents[k]) > i else pad_token for k in range(batch_size)]) #
    return sents_t


def word2id(sents, vocab):
    if type(sents[0]) == list:
        return [[vocab[w] for w in s] for s in sents]
    else:
        return [vocab[w] for w in sents]


def tensor_transform(linear, X):
    # X is a 3D tensor
    return linear(X.contiguous().view(-1, X.size(2))).view(X.size(0), X.size(1), -1)


class Actor(nn.Module):
    def __init__(self, input_size, bottle_size, out_size):
        super(Actor, self).__init__()
        self.affine = nn.Linear(input_size, bottle_size)
        self.relu = nn.ReLU()
        self.action = nn.Linear(bottle_size, out_size)
        self.sigm2 = nn.Sigmoid()
    
    def forward(self, inputs_hidden):
        aff1 = self.relu(self.affine(inputs_hidden))
        out = self.action(aff1)
        return self.sigm2(out)
         
         
class NMT(nn.Module):
    def __init__(self, args, vocab):
        super(NMT, self).__init__()

        self.args = args

        self.vocab = vocab

        self.src_embed = nn.Embedding(len(vocab.src), args.embed_size, padding_idx=vocab.src['<pad>'])
        self.tgt_embed = nn.Embedding(len(vocab.tgt), args.embed_size, padding_idx=vocab.tgt['<pad>'])

        self.encoder_lstm = nn.LSTM(args.embed_size, args.hidden_size, bidirectional=True, dropout=args.dropout)
        self.decoder_lstm = nn.LSTMCell(args.embed_size + args.hidden_size, args.hidden_size)

        # attention: dot product attention
        # project source encoding to decoder rnn's h space
        self.att_src_linear = nn.Linear(args.hidden_size * 2, args.hidden_size, bias=False)

        # transformation of decoder hidden states and context vectors before reading out target words
        # this produces the `attentional vector` in (Luong et al., 2015)
        self.att_vec_linear = nn.Linear(args.hidden_size * 2 + args.hidden_size, args.hidden_size, bias=False)

        # prediction layer of the target vocabulary
        self.readout = nn.Linear(args.hidden_size, len(vocab.tgt), bias=False)

        # dropout layer
        self.dropout = nn.Dropout(args.dropout)

        # initialize the decoder's state and cells with encoder hidden states
        self.decoder_cell_init = nn.Linear(args.hidden_size * 2, args.hidden_size)

        #self.actor = Actor(2 * args.hidden_size, 64, 2 * args.hidden_size)

    def forward(self, src_sents, src_sents_len, tgt_words, use_teacher_forcing=True, use_actor=False,\
                relax_beam = False, beam_size=2, temperature=1.0):
        src_encodings, init_ctx_vec = self.encode(src_sents, src_sents_len)
        if relax_beam:
            return self.relax_decode(src_encodings, init_ctx_vec,\
                                     tgt_sents=tgt_words, beam_size=beam_size, temperature=temperature)
        scores = self.decode(src_encodings, init_ctx_vec, tgt_words, use_teacher_forcing, use_actor)
        return scores

    def encode(self, src_sents, src_sents_len=None):
        """
        :param src_sents: (src_sent_len, batch_size), sorted by the length of the source
        :param src_sents_len: (src_sent_len)
        """
        # (src_sent_len, batch_size, embed_size)
        src_word_embed = self.src_embed(src_sents)#.permute(1,0))
        packed_src_embed = pack_padded_sequence(src_word_embed, src_sents_len)
        # output: (src_sent_len, batch_size, hidden_size)
        output, (last_state, last_cell) = self.encoder_lstm(packed_src_embed)
        output, _ = pad_packed_sequence(output)

        dec_init_cell = self.decoder_cell_init(torch.cat([last_cell[0], last_cell[1]], 1))
        dec_init_state = F.tanh(dec_init_cell)
        return output, (dec_init_state, dec_init_cell)

    def decode(self, src_encoding, dec_init_vec, tgt_sents, use_teacher_forcing=True, use_actor=False):
        """
        :param src_encoding: (src_sent_len, batch_size, hidden_size)
        :param dec_init_vec: (batch_size, hidden_size)
        :param tgt_sents: (tgt_sent_len, batch_size)
        :return:
        """
        init_state = dec_init_vec[0]
        init_cell = dec_init_vec[1]
        hidden = (init_state, init_cell)

        new_tensor = init_cell.data.new
        batch_size = src_encoding.size(1)

        # (batch_size, src_sent_len, hidden_size * 2)
        src_encoding = src_encoding.permute(1, 0, 2)
        # (batch_size, src_sent_len, hidden_size)
        src_encoding_att_linear = tensor_transform(self.att_src_linear, src_encoding)
        # initialize attentional vector
        att_tm1 = init_cell.new_zeros(batch_size, self.args.hidden_size,requires_grad=True)
        #print(att_tm1.requiers_grad)
        scores = []
        # start from `<s>`, until y_{T-1}
        if use_teacher_forcing:

            tgt_word_embed = self.tgt_embed(tgt_sents)#.permute(1, 0))

            for y_tm1_embed in tgt_word_embed.split(split_size=1):
                # input feeding: concate y_tm1 and previous attentional vector
                x = torch.cat([y_tm1_embed.squeeze(0), att_tm1], 1)

                # h_t: (batch_size, hidden_size)
                h_t, cell_t = self.decoder_lstm(x, hidden)
                h_t = self.dropout(h_t)

                ctx_t, alpha_t = self.dot_prod_attention(h_t, src_encoding, src_encoding_att_linear)

                att_t = F.tanh(self.att_vec_linear(torch.cat([h_t, ctx_t], 1)))   # E.q. (5)
                att_t = self.dropout(att_t)

                score_t = self.readout(att_t)   # E.q. (6)
                scores.append(score_t)

                att_tm1 = att_t
                hidden = h_t, cell_t
        else:
            scores = []
            t = 0

            eos = self.vocab.tgt['</s>']
            sample_ends = torch.tensor([0] * batch_size, device=device, dtype=torch.uint8)
            all_ones = torch.tensor([1] * batch_size, device=device, dtype=torch.uint8)

            eos_embd = self.tgt_embed(torch.tensor([eos] * batch_size, device=device))

            with torch.no_grad():
                y_0 = torch.tensor([self.vocab.tgt['<s>'] for _ in range(batch_size)], dtype=torch.long, device=device)
                samples = [y_0]
            if tgt_sents is not None:
                tgt_lens = [len(t) for t in tgt_sents.transpose(0,1)]
            else:
                tgt_lens = 50
            y_tm1 = samples[-1]
            y_tm1_embed = self.tgt_embed(y_tm1)

            while t < max(tgt_lens):#args.decode_max_time_step:
                t += 1

                # (sample_size)
                x = torch.cat([y_tm1_embed, att_tm1], 1)
                #TODO add parameter
                if use_actor:
                    z = self.actor(x)
                else:
                    z = torch.zeros_like(x)
                # h_t: (batch_size, hidden_size)
                h_t, cell_t = self.decoder_lstm(x + z, hidden)
                h_t = self.dropout(h_t)

                ctx_t, alpha_t = self.dot_prod_attention(h_t, src_encoding, src_encoding_att_linear)

                att_t = F.tanh(self.att_vec_linear(torch.cat([h_t, ctx_t], 1)))  # E.q. (5)
                att_t = self.dropout(att_t)

                score_t = self.readout(att_t)  # E.q. (6)
                scores.append(score_t)
                assert score_t.requires_grad
                p_t = F.softmax(score_t, dim=1)
                with torch.no_grad():
                    if args.sample_method == 'random':
                        y_t = torch.multinomial(p_t, num_samples=1).squeeze(1)
                    elif args.sample_method == 'greedy':
                        #_, y_t = torch.topk(p_t, k=1, dim=1)
                        y_t = torch.argmax(p_t,dim=-1)
                        y_tm1_embed = self.tgt_embed.weight[y_t.data]
                    elif args.sample_method == 'gumbel':
                        with torch.enable_grad():
                            gumb_dist = F.gumbel_softmax(score_t, tau=0.5, hard=self.args.st_gumbel)
                            y_tm1_embed = torch.matmul(gumb_dist, self.tgt_embed.weight)

                #samples[0] = y_t

                #sample_ends |= torch.eq(y_t, eos)
                # if torch.equal(y_tm1_embed, eos_embd):
                #     print("eoses")
                #     break
                # #TODO find appropriate method to break loop
                # #if torch.equal(sample_ends, all_ones):
                # #    break

                att_tm1 = att_t
                hidden = h_t, cell_t
            del sample_ends
            del all_ones, eos
        scores = torch.stack(scores)
        return scores

    def translate(self, src_sents, beam_size=None, to_word=True, args_new=None):
        global args
        """
        perform beam search
        TODO: batched beam search
        """
        if args_new:
            args = args_new
        if not type(src_sents[0]) == list:
            src_sents = [src_sents]
        if not beam_size:
            beam_size = args.beam_size

        src_sents_var = to_input_variable(src_sents, self.vocab.src)#.permute(1,0)

        src_encoding, dec_init_vec = self.encode(src_sents_var, [len(src_sents[0])])
        src_encoding_att_linear = tensor_transform(self.att_src_linear, src_encoding)

        init_state = dec_init_vec[0]
        init_cell = dec_init_vec[1]
        hidden = (init_state, init_cell)
        with torch.no_grad():
            att_tm1 = torch.zeros(1, self.args.hidden_size, device=device, requires_grad=True)
            hyp_scores = torch.zeros(1, device=device, requires_grad=True)

        eos_id = self.vocab.tgt['</s>']
        bos_id = self.vocab.tgt['<s>']
        tgt_vocab_size = len(self.vocab.tgt)

        hypotheses = [[bos_id]]
        completed_hypotheses = []
        completed_hypothesis_scores = []

        t = 0
        while len(completed_hypotheses) < beam_size and t < 157:#args.decode_max_time_step:
            t += 1
            hyp_num = len(hypotheses)

            expanded_src_encoding = src_encoding.expand(src_encoding.size(0), hyp_num, src_encoding.size(2))
            expanded_src_encoding_att_linear = src_encoding_att_linear.expand(src_encoding_att_linear.size(0), hyp_num, src_encoding_att_linear.size(2))
            with torch.no_grad():
                y_tm1 = torch.tensor([hyp[-1] for hyp in hypotheses], device=device)

                y_tm1_embed = self.tgt_embed(y_tm1)

                x = torch.cat([y_tm1_embed, att_tm1], 1)

            # h_t: (hyp_num, hidden_size)
            h_t, cell_t = self.decoder_lstm(x, hidden)
            h_t = self.dropout(h_t)
            ctx_t, alpha_t = self.dot_prod_attention(h_t, expanded_src_encoding.permute(1, 0, 2), expanded_src_encoding_att_linear.permute(1, 0, 2))

            att_t = F.tanh(self.att_vec_linear(torch.cat([h_t, ctx_t], 1)))
            att_t = self.dropout(att_t)

            score_t = self.readout(att_t)
            p_t = F.log_softmax(score_t,dim=-1)

            live_hyp_num = beam_size - len(completed_hypotheses)
            new_hyp_scores = (hyp_scores.unsqueeze(1).expand_as(p_t) + p_t).view(-1)
            top_new_hyp_scores, top_new_hyp_pos = torch.topk(new_hyp_scores, k=live_hyp_num)
            prev_hyp_ids = top_new_hyp_pos / tgt_vocab_size
            word_ids = top_new_hyp_pos % tgt_vocab_size
            # new_hyp_scores = new_hyp_scores[top_new_hyp_pos.data]
            new_hypotheses = []
            live_hyp_ids = []
            new_hyp_scores = []
            for prev_hyp_id, word_id, new_hyp_score in zip(prev_hyp_ids, word_ids, top_new_hyp_scores):
                #print("prev:{} word {}, new_score {}".format(prev_hyp_id, word_id, new_hyp_score))
                hyp_tgt_words = hypotheses[prev_hyp_id] + [word_id.item()]
                if word_id == eos_id:
                    completed_hypotheses.append(hyp_tgt_words)
                    completed_hypothesis_scores.append(new_hyp_score)
                else:
                    new_hypotheses.append(hyp_tgt_words)
                    live_hyp_ids.append(prev_hyp_id)
                    new_hyp_scores.append(new_hyp_score)
            if len(completed_hypotheses) == beam_size:
                break

            live_hyp_ids = torch.LongTensor(live_hyp_ids).to(device)
            if args.cuda:
                live_hyp_ids = live_hyp_ids.cuda()
            hidden = (h_t[live_hyp_ids], cell_t[live_hyp_ids])
            att_tm1 = att_t[live_hyp_ids]
            with torch.no_grad():
                hyp_scores = torch.tensor(new_hyp_scores, dtype=torch.float, device=device) # new_hyp_scores[live_hyp_ids]
                hypotheses = new_hypotheses

        if len(completed_hypotheses) == 0:
            completed_hypotheses = [hypotheses[0]]
            completed_hypothesis_scores = [0.0]

        if to_word:
            for i, hyp in enumerate(completed_hypotheses):
                completed_hypotheses[i] = [self.vocab.tgt.id2word[w] for w in hyp]

        ranked_hypotheses = sorted(zip(completed_hypotheses, completed_hypothesis_scores), key=lambda x: x[1], reverse=True)
        return [hyp for hyp, score in ranked_hypotheses]

    def translate_batch(self, src_batch, beam_size=2):
        """ Translation work in one batch """

        # Batch size is in different location depending on data.
        from beam import Beam

        # Help functions for working with beams and batches
        def var(a): return a

        def rvar(a): return var(a.repeat(1, beam_size, 1))

        def bottle(m):
            return m.view(n_remaining_sents * beam_size, -1)

        src_seq = src_batch
        batch_size = len(src_seq)
        n_remaining_sents = batch_size
        src_sents_len = [len(s) for s in src_seq]
        src_sents_var = to_input_variable(src_seq, self.vocab.src)

        # --- Encode data for beam
        enc_output, init_dec = self.encode(src_sents_var, src_sents_len)
        # (batch_size, src_sent_len, hidden_size * 2)
        enc_output = enc_output.permute(1, 0, 2)
        enc_output = enc_output.data.repeat(1, beam_size, 1).view(
                enc_output.size(0) * beam_size, enc_output.size(1), enc_output.size(2))

        # --- Prepare beams
        beams = [Beam(beam_size, self.vocab.tgt['<pad>'], self.vocab.tgt['<s>'], self.vocab.tgt['</s>']) for _ in range(batch_size)]
        beam_inst_idx_map = {
            beam_idx: inst_idx for inst_idx, beam_idx in enumerate(range(batch_size))}
        n_remaining_sents = batch_size

        src_encoding_att_linear = tensor_transform(self.att_src_linear, enc_output)
        att_tm1 = torch.zeros(beam_size * batch_size, self.args.hidden_size, device=device, requires_grad=False)
        init_state = bottle(rvar(init_dec[0].data))
        init_cell = bottle(rvar(init_dec[1].data))
        hidden = (init_state, init_cell)
        # - Decode
        #TODO len decoding
        for i in range(157):
            if all((b.done() for b in beams)):
                # all instances have finished their path to <\s>
                break
            # -- Preparing decoded data seq -- #
            #batch x beam x seq
            dec_partial_seq = torch.stack([
                b.get_current_state().to(device) for b in beams if not b.done()])

            # size: (batch * beam) x
            dec_partial_seq = dec_partial_seq.to(device)
            dec_embed = self.tgt_embed(dec_partial_seq).view(n_remaining_sents * beam_size,-1)
            x_dec = torch.cat([dec_embed, att_tm1], 1)
            # -- Decoding -- #

            #(hyp_num, hidden_size) x2
            h_t, cell_t = self.decoder_lstm(x_dec, hidden)
            h_t = self.dropout(h_t)

            ctx_t, alpha_t = self.dot_prod_attention(h_t, enc_output, src_encoding_att_linear)

            att_t = F.tanh(self.att_vec_linear(torch.cat([h_t, ctx_t], 1)))
            att_t = self.dropout(att_t)

            score_t = self.readout(att_t)
            out = F.log_softmax(score_t, dim=-1)

            # batch x beam x n_words
            word_lk = out.view(n_remaining_sents, beam_size, -1).contiguous()

            active_beam_idx_list = []
            for beam_idx in range(batch_size):
                if beams[beam_idx].done():
                    continue

                inst_idx = beam_inst_idx_map[beam_idx]
                if not beams[beam_idx].advance(word_lk[inst_idx]):
                    active_beam_idx_list += [beam_idx]

            # Update parameters
            if not active_beam_idx_list:
                # all instances have finished their path to <\s>
                break

            # find active indexes for the batch
            active_inst_idxs = torch.tensor(
                [beam_inst_idx_map[k] for k in active_beam_idx_list], dtype=torch.long, device=device)

            # update the idx mapping
            beam_inst_idx_map = {
                beam_idx: inst_idx for inst_idx, beam_idx in enumerate(active_beam_idx_list)}


            def update_active_enc_info(enc_info_var, active_inst_idxs):
                """Remove the encoder outputs of finished instances in one batch. """
                inst_idx_dim_size, *rest_dim_sizes = enc_info_var.size()
                inst_idx_dim_size = inst_idx_dim_size * len(active_inst_idxs) // n_remaining_sents
                new_size = (inst_idx_dim_size, *rest_dim_sizes)
                # select the active instances in batch
                original_enc_info_data = enc_info_var.data.view(
                    n_remaining_sents, -1, 2*self.args.hidden_size)
                active_enc_info_data = original_enc_info_data.index_select(0, active_inst_idxs)
                active_enc_info_data = active_enc_info_data.view(*new_size)
                return active_enc_info_data

            def update_active_seq(seq_var, active_inst_idxs):
                """Remove the sequence of finished instances in one batch. """

                inst_idx_dim_size, *rest_dim_sizes = seq_var.size()
                inst_idx_dim_size = inst_idx_dim_size * len(active_inst_idxs) // n_remaining_sents
                new_size = (inst_idx_dim_size, *rest_dim_sizes)

                # select the active instances in batch
                original_seq_data = seq_var.data.view(n_remaining_sents, beam_size,-1)
                active_seq_data = original_seq_data.index_select(0, active_inst_idxs)
                active_seq_data = active_seq_data.view(*new_size)
                return active_seq_data

            h_t = update_active_seq(h_t, active_inst_idxs)#.view(n_remaining_sents*beam_size, -1)
            cell_t = update_active_seq(cell_t, active_inst_idxs)#.view(n_remaining_sents*beam_size, -1)
            att_tm1 = update_active_seq(att_t, active_inst_idxs)#.view(n_remaining_sents*beam_size, -1)
            hidden = h_t, cell_t

            enc_output = update_active_enc_info(enc_output, active_inst_idxs)
            #src_encoding_att_linear = update_active_enc_info(src_encoding_att_linear,active_inst_idxs)
            src_encoding_att_linear = tensor_transform(self.att_src_linear, enc_output)

            # - update the remaining size
            n_remaining_sents = len(active_inst_idxs)
        # - Return useful information
        all_hyp, all_scores = [], []
        n_best = 3 #TODO  is n_best
        for beam_idx in range(batch_size):
            #hyps.append(beams[beam_idx].completed_hyps)
            scores, tail_idxs = beams[beam_idx].sort_scores()
            all_scores += [scores[:n_best].data.cpu().numpy()]
            hyps = [beams[beam_idx].get_hypothesis(i) for i in tail_idxs[:n_best]]
            all_hyp += [hyps]
            #all_hyp += [beams[beam_idx].completed_hyps]

        for i, batch_hyp in enumerate(all_hyp):
            for j, hyp in enumerate(batch_hyp):
                all_hyp[i][j] = [self.vocab.tgt.id2word[w.item()] for w in hyp]
                all_hyp[i][j].insert(0,'<s>')


        return [hyp for hyp in all_hyp]

    def relax_decode(self, enc_output, init_dec, tgt_sents=None, beam_size=2, temperature=1.0):
        # Help functions for working with beams and batches
        def var(a): return a

        def rvar(a): return var(a.repeat(1, beam_size, 1))

        def bottle(m):
            return m.view(n_remaining_sents * beam_size, -1)

        def unbottle(m):
            return m.view(n_remaining_sents,beam_size, -1)

        if beam_size != args.beam_size:
            print("Error in beam size", beam_size)
        batch_size =  enc_output.size(1)
        n_remaining_sents = batch_size
        # (batch_size, src_sent_len, hidden_size * 2)
        enc_output = enc_output.permute(1, 0, 2)

        enc_output = enc_output.repeat(1, beam_size, 1).view(
                enc_output.size(0) * beam_size, enc_output.size(1), enc_output.size(2))

        if tgt_sents is not None:
            tgt_lens = max([len(t) for t in tgt_sents.transpose(0, 1)])
        else:
            tgt_lens = 157

        # --- Prepare beams
        # from relax_beam import RelaxBeam
        # beams = [RelaxBeam(beam_size, self.vocab.tgt['<pad>'], self.vocab.tgt['<s>'], self.vocab.tgt['</s>']) for _ in range(batch_size)]
        # beam_inst_idx_map = {
        #     beam_idx: inst_idx for inst_idx, beam_idx in enumerate(range(batch_size))}
        n_remaining_sents = batch_size

        src_encoding_att_linear = tensor_transform(self.att_src_linear, enc_output)
        #att_tm1 = torch.zeros(beam_size * batch_size, self.args.hidden_size, device=device, requires_grad=True)
        #att_tm1 = torch.zeros(beam_size * batch_size, self.args.hidden_size, device=device, requires_grad=True)
        init_state = bottle(rvar(init_dec[0].data))
        init_cell = bottle(rvar(init_dec[1].data))
        att_tm1 = init_cell.new_zeros(batch_size * beam_size, self.args.hidden_size, requires_grad=True)

        hidden = (init_state, init_cell)

        torch.set_printoptions(threshold=500, edgeitems=30)

        def print_entropy_of_beam(probs):
            #prob = probs[:,0].view(n_remaining_sents, beam_size, -1)

            entropy = torch.mean(-probs[:,0] * torch.log(probs[:,0] + 1e-12))
            #
            # entropy1 = torch.mean(-probs[:,0] * torch.log(prob[0] + 1e-12))
            # entropy2 = torch.mean(-probs[:,0] * torch.log(probs[:,0] + 1e-12))
            # entropy3 = torch.mean(-probs[:,0] * torch.log(probs[:,0] + 1e-12))
            #
            # print("Entropy : 1{} \t 2:{} \t 3{}".format(entropy.item()))

            print("Entropy over beams: {}".format(entropy.item()))
            # print("Entropy of beams : 1:{}\t 2:{} \t 3:{}".format(ent1.item(), ent2.item(), ent3.item()))

        def continuous_topk(logits):
            """
            :param logits:
            :return: P array of peaked softmax matrix with size K x V
            """
            assert logits.size()[1] == beam_size
            new_logs = logits.view(n_remaining_sents, -1)
            m, marg = torch.topk(new_logs,beam_size,-1)
            m = m.unsqueeze_(2).to(device)
            lob = F.softmax(temperature * (-(new_logs.unsqueeze_(1) - m)**2), dim=-1) #
            # print(torch.max(lob,dim=-1))
            # print(torch.argmax(new_logs,dim=-1))
            # print(torch.argmax(lob, dim=-1)[:,0])

            # if not torch.equal(torch.argmax(lob, dim=-1)[:,0],torch.argmax(new_logs,dim=-1).view(-1, n_remaining_sents)[0]):
            #     print(torch.equal(torch.argmax(lob, dim=-1)[:,0],torch.argmax(new_logs,dim=-1).view(-1, n_remaining_sents)[0]))
            #     print(torch.topk(new_logs,beam_size,-1))
            #     print(torch.argmax(lob, dim=-1))

            return lob.view(n_remaining_sents, beam_size, beam_size, -1)

        #eos_embd = self.tgt_embed(torch.tensor([self.vocab.tgt['</s>']], device=device))

        # - Decode
        #TODO len decoding
        # with torch.enable_grad():
        #     dec_partial_seq = torch.stack([
        #         b.get_current_state().to(device) for b in beams if not b.done()])
        # dec_partial_seq = dec_partial_seq.to(device)
        # print(dec_partial_seq.requires_grad)
        # print(dec_partial_seq.size())


        #dec_embed = self.tgt_embed(dec_partial_seq).view(n_remaining_sents * beam_size,-1).to(device)
        #print(dec_embed.size())
        # y_0 = torch.tensor([self.vocab.tgt['<s>'] for _ in range(batch_size)], \
        #                    dtype=torch.long, device=device, requires_grad=True).repeat(1,beam_size,1).\
        #     view(n_remaining_sents * beam_size,-1).to(device)
        y_0 = torch.tensor([tgt_sents[0][i] for i in range(batch_size)],\
                           dtype=torch.long, device=device, requires_grad=True).repeat(1, beam_size, 1). \
            view(n_remaining_sents * beam_size, -1).to(device)

        dec_embed = self.tgt_embed(y_0).squeeze(1)
        logits = []
        new_scores = torch.zeros((n_remaining_sents, beam_size), device=device)
        #logs = torch.tensor((1,batch_size, beam_size, len(self.vocab.tgt)), device=device)
        scores = []
        for i in range(tgt_lens):
            # size: (batch * beam) x
            x_dec = torch.cat([dec_embed, att_tm1], 1)
            # -- Decoding -- #
            # (hyp_num, hidden_size) x2
            h_t, cell_t = self.decoder_lstm(x_dec, hidden)
            h_t = self.dropout(h_t)

            ctx_t, alpha_t = self.dot_prod_attention(h_t, enc_output, src_encoding_att_linear)

            att_t = F.tanh(self.att_vec_linear(torch.cat([h_t, ctx_t], 1)))
            att_t = self.dropout(att_t)

            score_t = self.readout(att_t)

            # batch x beam x n_words
            log = score_t.view(n_remaining_sents, beam_size, -1).contiguous()

            # if n_remaining_sents < batch_size:
            #     new_logit = logits[-1]
            #     print("n_remaining_sents", n_remaining_sents)
            #     exit()
            #     new_logit[n_remaining_sents].data = log.data
            #     logits.append(new_logit)
            # else:
            #     logits.append(log)
            #print(torch.equal(log[0],log[0][1]))
            s_tilda = log + new_scores[...,None]
            s_tilda = s_tilda.squeeze(2)
            #logits.append(log)

            P = continuous_topk(s_tilda)
            # if i == 0:
            #     print_entropy_of_beam(P.view(n_remaining_sents, beam_size,-1))

            #P = continuous_topk(score_t.view(n_remaining_sents,beam_size,-1).contiguous())
            s_tilda = s_tilda.squeeze(2)
            # batch x beam x n_words
            adv = torch.sum(P, dim=-2)
            # batch x beam x beam
            backpointer = torch.sum(P, dim=-1)
            dec_embed = bottle(torch.matmul(adv, self.tgt_embed.weight))

            new_scores = torch.stack([torch.sum(s_tilda.mul(P[:,k]), dim=1).sum(1) for k in range(beam_size)],dim=1)
            logits.append(adv)

            h_t = bottle(torch.bmm(backpointer, unbottle(h_t)))
            scores.append(new_scores)
            hidden = h_t, cell_t
            att_tm1 = att_t
            src_encoding_att_linear = tensor_transform(self.att_src_linear, enc_output)
            #- update the remaining size
            n_remaining_sents = batch_size

            #n_remaining_sents = len(active_inst_idxs)
        # - Return logits and scores for all session # TODO: what scores should be

        return torch.stack(logits, dim=0), torch.stack(scores,dim=0)



    def sample(self, src_encoding, dec_init_vec, sample_size=None, to_word=False):
        init_state = dec_init_vec[0]
        init_cell = dec_init_vec[1]
        hidden = (init_state, init_cell)

        new_tensor = init_cell.data.new
        batch_size = src_encoding.size(1)

        # (batch_size, src_sent_len, hidden_size * 2)
        src_encoding = src_encoding.permute(1, 0, 2)
        # (batch_size, src_sent_len, hidden_size)
        src_encoding_att_linear = tensor_transform(self.att_src_linear, src_encoding)#.permute(1,0,2)
        # initialize attentional vector
        att_tm1 = new_tensor(batch_size, self.args.hidden_size).zero_()

        with torch.no_grad():
            y_0 = torch.tensor([self.vocab.tgt['<s>'] for _ in range(batch_size)], dtype=torch.long, device=device)
            samples = [y_0]

        eos = self.vocab.tgt['</s>']
        # eos_batch = torch.LongTensor([eos] * batch_size)
        sample_ends = torch.tensor([0] * batch_size, device=device, dtype=torch.uint8)
        all_ones = torch.tensor([1] * batch_size, device=device, dtype=torch.uint8)
        #eos = torch.tensor(self.vocab.tgt['</s>'], dtype=torch.float)
        scores = []
        t = 0

        while t < args.decode_max_time_step:
            t += 1

            # (sample_size)
            y_tm1 = samples[-1]

            y_tm1_embed = self.tgt_embed(y_tm1)
            x = torch.cat([y_tm1_embed, att_tm1], 1)
            # h_t: (batch_size, hidden_size)
            h_t, cell_t = self.decoder_lstm(x, hidden)
            h_t = self.dropout(h_t)

            ctx_t, alpha_t = self.dot_prod_attention(h_t, src_encoding, src_encoding_att_linear)

            att_t = F.tanh(self.att_vec_linear(torch.cat([h_t, ctx_t], 1)))  # E.q. (5)
            att_t = self.dropout(att_t)

            score_t = self.readout(att_t)  # E.q. (6)
            scores.append(score_t)
            p_t = F.softmax(score_t, dim=1)

            with torch.no_grad():
                if args.sample_method == 'random':
                    y_t = torch.multinomial(p_t, num_samples=1).squeeze(1)
                elif args.sample_method == 'greedy':
                    _, y_t = torch.topk(p_t, k=1, dim=1)
                    y_t = y_t.squeeze(1)

            samples.append(y_t)

            sample_ends |= torch.eq(y_t, eos)
            if torch.equal(sample_ends, all_ones):
                break

            att_tm1 = att_t
            hidden = h_t, cell_t
        del sample_ends
        del all_ones, eos
        # post-processing
        if not to_word:
            samples = torch.stack(samples[1:]).to(device).permute(1,0)
            return samples
        else:
            completed_samples = [list([list() for _ in range(sample_size)]) for _ in range(src_sents_num)]
            for y_t in samples:
                for i, sampled_word in enumerate(y_t.cpu().data):
                    src_sent_id = i % src_sents_num
                    sample_id = i / src_sents_num
                    if len(completed_samples[src_sent_id][sample_id]) == 0 or completed_samples[src_sent_id][sample_id][-1] != eos:
                        completed_samples[src_sent_id][sample_id].append(sampled_word)

            if to_word:
                for i, src_sent_samples in enumerate(completed_samples):
                    completed_samples[i] = word2id(src_sent_samples, self.vocab.tgt.id2word)

            return completed_samples

    def attention(self, h_t, src_encoding, src_linear_for_att):
        # (1, batch_size, attention_size) + (src_sent_len, batch_size, attention_size) =>
        # (src_sent_len, batch_size, attention_size)
        att_hidden = F.tanh(self.att_h_linear(h_t).unsqueeze(0).expand_as(src_linear_for_att) + src_linear_for_att)

        # (batch_size, src_sent_len)
        att_weights = F.softmax(tensor_transform(self.att_vec_linear, att_hidden).squeeze(2).permute(1, 0))

        # (batch_size, hidden_size * 2)
        ctx_vec = torch.bmm(src_encoding.permute(1, 2, 0), att_weights.unsqueeze(2)).squeeze(2)

        return ctx_vec, att_weights

    def dot_prod_attention(self, h_t, src_encoding, src_encoding_att_linear, mask=None):
        """
        :param h_t: (batch_size, hidden_size)
        :param src_encoding: (batch_size, src_sent_len, hidden_size * 2)
        :param src_encoding_att_linear: (batch_size, src_sent_len, hidden_size)
        :param mask: (batch_size, src_sent_len)
        """
        # (batch_size, src_sent_len)
        att_weight = torch.bmm(src_encoding_att_linear, h_t.unsqueeze(2)).squeeze(2)
        if mask:
            att_weight.data.masked_fill_(mask, -float('inf'))

        att_weight = F.softmax(att_weight, dim=1)

        att_view = (att_weight.size(0), 1, att_weight.size(1))
        # (batch_size, hidden_size)
        ctx_vec = torch.bmm(att_weight.view(*att_view), src_encoding).squeeze(1)

        return ctx_vec, att_weight

    def save(self, path):
        print('save parameters to [%s]' % path, file=sys.stderr)
        params = {
            'args': self.args,
            'vocab': self.vocab,
            'state_dict': self.state_dict()
        }
        os.makedirs(os.path.dirname(path), exist_ok=True)
        torch.save(params, path)


def to_input_variable(sents, vocab, cuda=False, is_test=False):
    """
    return a tensor of shape (src_sent_len, batch_size)
    """
    word_ids = word2id(sents, vocab)
    sents_t = input_transpose(word_ids, vocab['<pad>'], 50)
    sents_var = torch.tensor(sents_t, dtype=torch.long, device=device)
    return sents_var


def evaluate_loss(model, data, crit, use_teacher_forcing=True):
    model.eval()
    cum_loss = 0.
    cum_tgt_words = 0.
    for src_sents, tgt_sents in data_iter(data, batch_size=args.batch_size, shuffle=False):
        pred_tgt_word_num = sum(len(s[1:]) for s in tgt_sents) # omitting leading `<s>`
        src_sents_len = [len(s) for s in src_sents]
        src_sents_var = to_input_variable(src_sents, model.vocab.src)#.permute(1,0)
        tgt_sents_var = to_input_variable(tgt_sents, model.vocab.tgt)#.permute(1,0)

        # (tgt_sent_len, batch_size, tgt_vocab_size)
        if use_teacher_forcing:
            scores = model(src_sents_var, src_sents_len, tgt_sents_var[:-1],use_teacher_forcing)
        else:
            scores = model(src_sents_var, src_sents_len, tgt_sents_var[:-1])
        loss = crit(scores.view(-1, scores.size(2)), tgt_sents_var[1:].contiguous().view(-1))

        cum_loss += loss.item()
        cum_tgt_words += pred_tgt_word_num
    if cum_tgt_words == 0:
        return 0
    loss = cum_loss / cum_tgt_words
    return loss


def init_training(args):
    if args.load_model:
        print('load model from [%s]' % args.load_model, file=sys.stderr)
        params = torch.load(args.load_model, map_location=lambda storage, loc: storage)
        vocab = params['vocab']
        saved_args = params['args']
        state_dict = params['state_dict']

        model = NMT(saved_args, vocab)
        model.load_state_dict(state_dict)
    else:
        print(args.vocab)
        vocab = torch.load(args.vocab)
        model = NMT(args, vocab)

    model.train()
    model.to(device)

    if args.uniform_init:
        print('uniformly initialize parameters [-%f, +%f]' % (args.uniform_init, args.uniform_init), file=sys.stderr)
        for p in model.parameters():
            p.data.uniform_(-args.uniform_init, args.uniform_init)

    vocab_mask = torch.ones(len(vocab.tgt))
    vocab_mask[vocab.tgt['<pad>']] = 0
    nll_loss = nn.NLLLoss(weight=vocab_mask, size_average=False).to(device)
    cross_entropy_loss = nn.CrossEntropyLoss(weight=vocab_mask, size_average=False).to(device)

    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    return vocab, model, optimizer, nll_loss, cross_entropy_loss


def init_custom_train(args):
    if args.load_model:
        print('load model from [%s]' % args.load_model, file=sys.stderr)
        params = torch.load(args.load_model, map_location=lambda storage, loc: storage)
        vocab = params['vocab']
        saved_args = params['args']
        state_dict = params['state_dict']

        model = NMT(saved_args, vocab)
        model.load_state_dict(state_dict)
    else:
        vocab = torch.load(args.vocab)
        model = NMT(args, vocab)

    model.train()
    # if args.uniform_init:
    #     print('uniformly initialize parameters [-%f, +%f]' % (args.uniform_init, args.uniform_init), file=sys.stderr)
    #     for p in model.parameters():
    #         p.data.uniform_(-args.uniform_init, args.uniform_init)

    vocab_mask = torch.ones(len(vocab.tgt))
    vocab_mask[vocab.tgt['<pad>']] = 0
    nll_loss = nn.NLLLoss(weight=vocab_mask, size_average=False, reduce=True).to(device)
    cross_entropy_loss = nn.CrossEntropyLoss(weight=vocab_mask, size_average=False).to(device)
    if args.mode == "train_actor":
        model.actor = Actor(2 * args.hidden_size, 64, 2 * args.hidden_size)
        for i in model.parameters():
            i.requires_grad = False
        for i in model.actor.parameters():
            i.requires_grad = True
        optimizer = torch.optim.Adam(model.actor.parameters(), lr=args.lr_warm)
    else:
        # for i in model.actor.parameters():
        #     i.requires_grad = False
        optimizer = torch.optim.Adam(model.parameters(), lr=args.lr_warm)
    model.to(device)

    return vocab, model, optimizer, nll_loss, cross_entropy_loss


class MySoftmax(nn.Module):
    def forward(self, input_):
        batch_size = input_.size()[0]
        output_ = F.softmax(input_,dim=2)
        #output_ = torch.stack([F.softmax(input_[i],1) for i in range(batch_size)], 0)
        return output_


def custom_train(args):
    """
    Train bleu lower bound
    """
    train_data_src = read_corpus(args.train_src, source='src')
    train_data_tgt = read_corpus(args.train_tgt, source='tgt')

    dev_data_src = read_corpus(args.dev_src, source='src')
    dev_data_tgt = read_corpus(args.dev_tgt, source='tgt')

    train_data = list(zip(train_data_src, train_data_tgt))
    dev_data = list(zip(dev_data_src, dev_data_tgt))

    vocab, model, optimizer, nll_loss, cross_entropy_loss = init_custom_train(args)
    print("vocab_size", vocab.tgt)
    train_iter = patience = cum_loss = report_loss = cum_tgt_words = report_tgt_words = 0
    cum_examples = cum_batches = report_examples = epoch = valid_num = best_model_iter = 0
    hist_valid_scores = []
    train_time = begin_time = time.time()
    print('begin custom training')
    print("batch_size", args.batch_size)
    # for i in model.actor.parameters():
    #     i.requires_grad = False

    fmt = {'bleu_loss':'.5e'}
    logger = Logger("bleu_with_tf", fmt=fmt)

    while True:
        epoch += 1
        if epoch > args.max_epoch:
            exit(0)
        print('current epoch', epoch)
        di = data_iter(train_data, batch_size=args.batch_size)
        #train_data = list(zip(train_data_src, train_data_tgt))
        tqdm_bar = tqdm(total=len(train_data))

        for src_sents, tgt_sents in di:
            # print(epoch, '--', train_iter)
            train_iter += 1

            src_sents_var = to_input_variable(src_sents, vocab.src)
            tgt_sents_var = to_input_variable(tgt_sents, vocab.tgt)

            batch_size = len(src_sents)
            src_sents_len = [len(s) for s in src_sents]
            pred_tgt_word_num = sum(len(s[1:]) for s in tgt_sents) # omitting leading `<s>`

            optimizer.zero_grad()

            # (tgt_sent_len, batch_size, tgt_vocab_size)
            scores = model(src_sents_var, src_sents_len, tgt_sents_var[:-1], use_teacher_forcing=args.use_teacher_forcing, use_actor=False)
            scores_numpy = scores.data.cpu().numpy()
            tgt_sents_numpy = tgt_sents_var.data.cpu().numpy()
            eos = model.vocab.tgt['</s>']
            bos = model.vocab.tgt['<s>']
            def _find_lentghs(sent):
                """ sent (sent_len x batch_size) """
                tmp = sent == eos
                return np.argmax(tmp, axis=0) + 1

            greedy_hypo = np.argmax(scores_numpy, axis=2)
            hypo_lengths = _find_lentghs(greedy_hypo) # no bos
            ref_lengths = _find_lentghs(tgt_sents_numpy) - 1 #because of bos

            regular_sm = MySoftmax()
            probs = regular_sm(scores.permute(1, 0, 2))#[batch_size, sents_length , target_vocab]
            refs = tgt_sents_var[1:].permute(1, 0)
            r = refs.data.cpu().numpy().tolist()

            if not args.sentence_bleu:
                bleu_loss, _ = bleu(probs, r,\
                            torch.tensor(hypo_lengths.tolist(),dtype=torch.long, device=device),\
                            ref_lengths.tolist(), smooth=True,device=device)
            else:
                bleu_stacked = torch.stack([bleu(torch.stack([probs[j][:ref_lengths.tolist()[j]]]), \
                                [r[j][:ref_lengths.tolist()[j]]],torch.tensor([hypo_lengths.tolist()[j]],dtype=torch.long, device=device),
                                [ref_lengths.tolist()[j]], smooth=True, device=device)[0] for j in range(batch_size)])

                bleu_loss = torch.mean(bleu_stacked)

            # unnecessary regularizer
            # sample_logp = F.log_softmax(scores.permute(1,0,2), dim=-1)
            #
            # entropy = - (sample_logp * torch.exp(sample_logp)).sum(dim=-1)
            # greedy_sample = torch.max(probs, dim=2)[1]
            #
            # mask = infer_mask(greedy_sample, eos)
            #
            # reg = - 0.0005 * torch.sum(entropy * mask) / torch.sum(mask)
            #reg = -0.01 * torch.sum(entropy )

            # print(bleu_loss)
            # word_loss = cross_entropy_loss(scores.view(-1, scores.size(2)), tgt_sents_var[1:].view(-1))
            # loss = word_loss / batch_size

            loss = bleu_loss
            # a = list(model.parameters())[0].clone()

            # word_loss_val = word_loss.data[0]
            # loss_val = loss.data[0]
            word_loss_val = bleu_loss.item()
            loss_val = loss.item()
            loss.backward()

            # clip gradient
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad)
            optimizer.step()

            # print(loss.grad)
            # b = list(model.parameters())[0].clone()
            #
            # print(torch.equal(a.data, b.data))
            # exit()
            logger.add_scalar(train_iter,'bleu_loss', bleu_loss.item())
            report_loss += word_loss_val
            cum_loss += word_loss_val
            report_tgt_words += pred_tgt_word_num
            cum_tgt_words += pred_tgt_word_num
            report_examples += batch_size
            cum_examples += batch_size
            cum_batches += batch_size

            if train_iter % args.log_every == 0:
                logger.iter_info()
                logger.save()
                print('epoch %d, iter %d, avg. loss %.2f, avg. ppl %.2f ' \
                      'cum. examples %d, speed %.2f words/sec, time elapsed %.2f sec' % (epoch, train_iter,
                                                                                         report_loss / report_examples,
                                                                                         np.exp(report_loss / report_tgt_words),
                                                                                         cum_examples,
                                                                                         report_tgt_words / (time.time() - train_time),
                                                                                         time.time() - begin_time), file=sys.stderr)

                train_time = time.time()
                report_loss = report_tgt_words = report_examples = 0.
            # perform validation
            if train_iter % args.valid_niter == 0:
                print('epoch %d, iter %d, cum. loss %.2f, cum. ppl %.2f cum. examples %d' % (epoch, train_iter,
                                                                                         cum_loss / cum_batches,
                                                                                         np.exp(cum_loss / cum_tgt_words),
                                                                                         cum_examples), file=sys.stderr)

                cum_loss = cum_batches = cum_tgt_words = 0.
                valid_num += 1

                print('begin validation ...', file=sys.stderr)
                model.eval()

                # compute dev. ppl and bleu

                dev_loss = evaluate_loss(model, dev_data, cross_entropy_loss)
                dev_ppl = np.exp(dev_loss)

                if args.valid_metric in ['bleu', 'word_acc', 'sent_acc']:
                    dev_hyps, dev_tgt= decode(model, dev_data, verbose=False, batch_size=1)
                    dev_hyps = [hyps[0] for hyps in dev_hyps]
                    if args.valid_metric == 'bleu':
                        # valid_metric = get_bleu([tgt for src, tgt in dev_data], dev_hyps)
                        valid_metric = compute_bleu([[tgt] for tgt in dev_tgt], dev_hyps)[0]
                        # print('-'*10)
                        # print('compute_bleu', valid_metric)
                        # print('-'*10)
                    else:
                        valid_metric = get_acc([tgt for src, tgt in dev_data], dev_hyps, acc_type=args.valid_metric)
                    print('validation: iter %d, dev. ppl %f, dev. %s %f' % (train_iter, dev_ppl, args.valid_metric, valid_metric),
                          file=sys.stderr)
                else:
                    valid_metric = -dev_ppl
                    print('validation: iter %d, dev. ppl %f' % (train_iter, dev_ppl),
                          file=sys.stderr)

                model.train()
                print(hist_valid_scores)
                is_better = len(hist_valid_scores) == 0 or valid_metric > max(hist_valid_scores)
                is_better_than_last = len(hist_valid_scores) == 0 or valid_metric > hist_valid_scores[-1]
                hist_valid_scores.append(valid_metric)
                if valid_num > args.save_model_after:
                    model_file = args.save_to
                    if is_better:
                        print('save model to [%s]' % model_file, file=sys.stderr)
                        model.save(model_file)

                if (not is_better_than_last) and args.lr_decay:
                    lr = optimizer.param_groups[0]['lr'] * args.lr_decay
                    print('decay learning rate to %f' % lr, file=sys.stderr)
                    optimizer.param_groups[0]['lr'] = lr

                if is_better:
                    patience = 0
                    best_model_iter = train_iter

                    if valid_num > args.save_model_after:
                        print('save currently the best model ..', file=sys.stderr)
                        model_file_abs_path = os.path.abspath(model_file)
                        symlin_file_abs_path = os.path.abspath(args.save_to + '.bin')
                        os.system('ln -sf %s %s' % (model_file_abs_path, symlin_file_abs_path))
                else:
                    patience += 1
                    print('hit patience %d' % patience, file=sys.stderr)
                    if patience == args.patience:
                        print('early stop!', file=sys.stderr)
                        print('the best model is from iteration [%d]' % best_model_iter, file=sys.stderr)
                        exit(0)
            tqdm_bar.update(batch_size)
        tqdm_bar.close()


def relax_train(args):
    """
    Train bleu lower bound
    """
    train_data_src = read_corpus(args.train_src, source='src')
    train_data_tgt = read_corpus(args.train_tgt, source='tgt')

    dev_data_src = read_corpus(args.dev_src, source='src')
    dev_data_tgt = read_corpus(args.dev_tgt, source='tgt')

    train_data = list(zip(train_data_src, train_data_tgt))
    dev_data = list(zip(dev_data_src, dev_data_tgt))

    vocab, model, optimizer, nll_loss, cross_entropy_loss = init_custom_train(args)
    #vocab, model, optimizer, nll_loss, cross_entropy_loss = init_training(args)

    print("vocab_size", vocab.tgt)
    train_iter = patience = cum_loss = report_loss = cum_tgt_words = report_tgt_words = 0
    cum_examples = cum_batches = report_examples = epoch = valid_num = best_model_iter = 0
    hist_valid_scores = []
    train_time = begin_time = time.time()
    print('begin custom training')
    fmt = {'bleu_loss':'.5e'}
    logger = Logger("relax-train-bleu_beam_"+ str(args.beam_size), fmt=fmt)
    while True:
        epoch += 1
        if epoch > args.max_epoch:
            exit(0)
        print('current epoch', epoch)
        di = data_iter(train_data, batch_size=args.batch_size)
        #train_data = list(zip(train_data_src, train_data_tgt))
        tqdm_bar = tqdm(total=len(train_data))

        temperature = 1
        for src_sents, tgt_sents in di:
            # print(epoch, '--', train_iter)
            train_iter += 1

            src_sents_var = to_input_variable(src_sents, vocab.src)
            tgt_sents_var = to_input_variable(tgt_sents, vocab.tgt)

            batch_size = len(src_sents)
            src_sents_len = [len(s) for s in src_sents]
            pred_tgt_word_num = sum(len(s[1:]) for s in tgt_sents) # omitting leading `<s>`

            if train_iter % 200 == 0:
                temperature = temperature + 250
            #     print("Temperature:", temperature)

            optimizer.zero_grad()
            # (tgt_sent_len, batch_size, tgt_vocab_size)
            probs, beam_scores = model(src_sents_var, src_sents_len, tgt_sents_var[:-1], use_teacher_forcing=False,\
                                        relax_beam=True, beam_size=args.beam_size, temperature=temperature)

            probs = probs.permute(0,2,1,3)
            beam_scores = beam_scores.permute(1,0, 2)

            scores_numpy = probs.data.cpu().numpy()
            tgt_sents_numpy = tgt_sents_var.data.cpu().numpy()
            eos = model.vocab.tgt['</s>']
            bos = model.vocab.tgt['<s>']

            if train_iter == 1800:
                exit()

            def _find_lentghs(sent):
                """ sent (sent_len x batch_size) """
                tmp = sent == eos
                lens = np.argmax(tmp, axis=0)
                #return lens + 1
                return np.where(lens > 0, lens, sent.shape[0] - 1) + 1

            greedy_hypo = np.argmax(scores_numpy, axis=-1) # tgt \s - is 3
            probs = probs.permute(1, 2, 0, 3)

            hypo_lengths = _find_lentghs(greedy_hypo) # no bos
            ref_lengths = _find_lentghs(tgt_sents_numpy) - 1 #because of bos

            #probs = F.softmax(scores, dim=-1).to(device)
            #max_probs = torch.log(probs).sum(dim=-2)
            refs = tgt_sents_var[1:].permute(1, 0)
            r = refs.data.cpu().numpy().tolist()

            # TODO remove the code below
            # for k in range(args.beam_size):
            #     for i in range(batch_size):
            #         hyps_probs = probs[k,i]
            #         eos_probs = torch.stack([hyps_probs[j][eos] for j in range(probs.size()[2])])
            #         print(hypo_lengths[k,i])
            #         print(ref_lengths[i])
            #         print(greedy_hypo[:,:,0])
            #         print(r[0])
            #         eos_log_probs = torch.stack([torch.sum(torch.log(1 - eos_probs[:j-1])) + torch.log(eos_probs[j-1]) for j in range(probs.size()[2])])
            #         print(eos_log_probs)
            #         print(torch.argmax(eos_log_probs))
            #
            #         #print(torch.stack([torch.sum(torch.log(1 - eos_probs[:j])) for j in range(probs.size()[2])]).size())
            #
            #         #print([[torch.sum(torch.log(1 - prob_ar[eos])) for prob_ar in hyps_probs[:j]] for j in range(probs.size()[2])])
            #         print(eos_probs)
            #         exit()

            # print("bleu:",bleu(torch.stack([probs[0][1]]), [r[1]], torch.tensor([hypo_lengths[0].tolist()[1]],dtype=torch.long, device=device),\
            #                    [ref_lengths.tolist()[1]], smooth=True,device=device)[0])


            # # print(torch.stack([bleu([probs[0][k]], [r[k]], torch.tensor([hypo_lengths[0].tolist()[k]],dtype=torch.long, device=device),\
            #                 ref_lengths.tolist()[k], smooth=True,device=device)[0] for k in range(batch_size)], dim=0))
            # exit()
            # print(bleu(probs[1], r, torch.tensor(hypo_lengths[1].tolist(), dtype=torch.long, device=device), \
            #            ref_lengths.tolist(), smooth=True, device=device)[0])
            #print(probs[0].size())
            #print(probs[0][0][:hypo_lengths[0].tolist()[0]])


            #hypo_lengths = torch.tensor(hypo_lengths, dtype=torch.long, device=device, requires_grad=True)
            beam_scores = torch.stack(
                    [torch.stack([beam_scores[k][hypo_lengths[i,k] - 1][i]/int(hypo_lengths[i,k]) for k in range(batch_size)])
                    for i in range(args.beam_size)]).transpose(0, 1)
            #IndexError: index 47 is out of bounds for dimension 0 with size 47
            # beam_scores = torch.stack([torch.tensor([beam_scores[k][hypo_lengths[i][k] - 1][i] for k in range(batch_size)], \
            #                                         requires_grad=True, device=device)\
            #                            for i in range(args.beam_size)]).transpose(0,1)

            # TODO: remove priority on the first beam in test time
            sample_logp = torch.log(probs + 1e-6)
            # first_eos = torch.max(hypo_lengths,dim=0)
            if args.sentence_bleu:

                # find max probability for eos
                # length x beam x batch
                eos_probs = torch.stack([probs[:, :, j, eos] for j in range(probs.size()[2])])
                eos_log_probs = torch.stack([torch.log(eos_probs[0] + 1e-8)] +
                                            [torch.sum(torch.log(1 - eos_probs[:j - 1] + 1e-5), dim=0) + \
                                             torch.log(eos_probs[j - 1] + 1e-8) for j in
                                             range(2, probs.size()[2])])  # + [eos_true]
                eos_exp_probs = torch.exp(eos_log_probs)
                prob_sum_prev = torch.sum(eos_exp_probs, dim=0)
                exp_probs = torch.cat([torch.exp(eos_log_probs), \
                                       (torch.ones_like(eos_exp_probs[-1]) - prob_sum_prev).unsqueeze(0)])

                expected_length = torch.einsum('ikd,i->kd', (exp_probs \
                                                                 , torch.arange(1, probs.size()[2] + 1).to(device)))

                with torch.no_grad():
                    # beam x batch
                    id_eoses = torch.ceil(expected_length).int().data.cpu().numpy()
                    # id_eoses = 1 + torch.argmax(eos_log_probs, dim=0) # equal to hypo
                    #                print("expected length:", torch.exp(eos_log_probs[:,0,0]).dot(torch.arange(1, probs.size()[2] + 1).to(device)))
                total_eos_probs = torch.max(exp_probs, dim=0)[0]


                beam_scores = F.softmax(beam_scores, dim=-1)
                #print(beam_scores[0])
                with torch.no_grad():
                    best_beam_ids = torch.max(beam_scores,dim=1)[1].data.cpu().numpy()
                    #best_beam_ids = [0]*args.batch_size
                #print("best_beam_ids", best_beam_ids.requires_grad)
                if args.beam_size != 1:
                    # bleu_stacked = torch.stack([torch.tensor([bleu(torch.stack([probs[k][j][:hypo_lengths[best_beam_ids[j]][j]]]),\
                    #                  [r[j][:ref_lengths.tolist()[j]]],torch.tensor([hypo_lengths[k].tolist()[j]],dtype=torch.long, device=device,requires_grad=True),\
                    #                  [ref_lengths.tolist()[j]], smooth=True,device=device)[0] \
                    #                               for j in range(batch_size)], device=device, requires_grad=True) for k in range(args.beam_size) ])#.transpose(0,1)

                    bleu_result = []
                    if torch.sum(torch.isnan(eos_log_probs)):
                        print(eos_probs)
                        print(exp_probs[:,0,3])
                        exit()
                    #print(total_eos_probs)
                    #print(total_eos_probs.size())

                    for k in range(args.beam_size):
                        #best_beam_ids[j]
                        bleu_result.append(torch.stack([total_eos_probs[k,j]*bleu(torch.stack([probs[k][j][:id_eoses[k,j]]]), \
                                                     [r[j][:ref_lengths[j]]],
                                                     torch.stack([expected_length[k][j]]),
                                                     [ref_lengths[j]], smooth=True, device=device)[0] for j in
                                                range(batch_size)]))

                    bleu_stacked = torch.stack(bleu_result)

                    # compute entropy
                        # train_ppl = cross_entropy_loss(probs[1].view(-1, scores.size(3)), tgt_sents_var[1:].contiguous().view(-1)).item()/np.sum(ref_lengths)
                        #
                        # ce_stacked = torch.stack([cross_entropy_loss(probs[k].contiguous().view(-1, scores.size(3)), \
                        #                                              tgt_sents_var[1:].contiguous().view(-1)) for k in
                        #                           range(args.beam_size)]).unsqueeze(1)

                    #min_bleu = torch.min(torch.sum(bleu_stacked,dim=-1)/batch_size)

                    logger.add_scalar(train_iter,'bleu_best', torch.min(torch.sum(bleu_stacked,dim=-1)/batch_size).item())
                    logger.add_scalar(train_iter,"all_bleu", bleu_stacked)


                    if train_iter % 500 ==0:
                        #print(total_eos_probs)
                        print(expected_length[:,0])
                        print(beam_scores[0])
                        print(bleu_stacked[:,0])
                        print(greedy_hypo[:max(id_eoses[:,0]),:,0])
                        print(r[0][:ref_lengths[0]])

                    # Corpus bleu test
#                     corpus_bl = []
# #                    print(torch.stack(probs[0,:][hypo_lengths[0]]).size())
#                     for k in range(args.beam_size):
#                         #prob = torch.stack([probs[k][j][:hypo_lengths[k][j]] for j in range(batch_size)])
#                         resr = []
#                         resp = []
#                         for j in range(batch_size):
#                             resp.append(probs[k][j][:hypo_lengths[k][j]])
#                             resr.append(r[j][:ref_lengths[j]])
#
#                         #resr = [ r[j][:ref_lengths[j]] for j in range(batch_size)]
#                         #print(torch.from_numpy(np.array(resr)))
#                         corpus_bl.append(
#                             bleu(probs[k],r,torch.tensor(hypo_lengths[k], dtype=torch.long,
#                                                                   device=device)
#                                    ,ref_lengths, smooth=True, device=device)[0]
#                         )

                    #beam_scores = F.softmax(-bleu_stacked, dim=0).transpose(0,1)

                    # bleu_stacked = torch.stack([torch.tensor([bleu(torch.stack([probs[0][j][:hypo_lengths[0].tolist()[j]]]), \
                    #                                  [r[j][:ref_lengths.tolist()[j]]],
                    #                                  torch.tensor([hypo_lengths[0].tolist()[j]], dtype=torch.long,
                    #                                               device=device),
                    #                                  [ref_lengths.tolist()[j]], smooth=True, device=device)[0] for j in
                    #                             range(batch_size)], \
                    #                                          device=device, requires_grad=True
                    #                                          )])

                    #print(bleu_stacked.size())
                    #print(bleu_stacked.repeat((args.beam_size,1)).size())
                    #bleu_stacked = bleu_stacked.repeat((args.beam_size,1)).reshape(args.beam_size,-1)
                else:
                    #torch.tensor([hypo_lengths[0].tolist()[j]], dtype=torch.long, device=device),
                    bleu_stacked = torch.stack([total_eos_probs[0,j]*bleu(torch.stack([probs[0][j][:id_eoses[0][j]]]), \
                                                     [r[j][:ref_lengths.tolist()[j]]], torch.stack([expected_length[0][j]]),
                                                     [ref_lengths[j]], smooth=True, device=device)[0] for j in
                                                range(batch_size)])
                    bleu_stacked = bleu_stacked.unsqueeze(0)

                    # unnecessary regularizer
                    # betta_entropy = 0.01
                    # sample_logp = F.log_softmax(scores, dim=-1)
                    # #part_entropy = (sample_logp * torch.exp(sample_logp)).sum(dim=-1)
                    # #print(torch.stack([torch.sum(part_entropy[:,j,:id_eoses[j]],dim=-1) for j in range(batch_size)]).size())
                    #
                    # entropy = - betta_entropy  * (sample_logp * torch.exp(sample_logp)).sum(dim=-1).sum(-1)
                    # bleu_stacked = bleu_stacked_empty + entropy
                    # bleu_stacked = torch.stack([torch.tensor([bleu(torch.stack([probs[0][j][:ref_lengths.tolist()[j]]]), \
                    #                                                [r[j][:ref_lengths.tolist()[j]]],
                    #                                                torch.tensor([hypo_lengths[0].tolist()[j]],
                    #                                                             dtype=torch.long, device=device,
                    #                                                             requires_grad=True), \
                    #                                                [ref_lengths.tolist()[j]], smooth=True,
                    #                                                device=device)[0] \
                    #                                           for j in range(batch_size)], device=device,
                    #                                          requires_grad=True) for k in range(args.beam_size)])

                bleu_loss = torch.trace(torch.mm(beam_scores, bleu_stacked))/batch_size
                #bleu_loss = min_bleu
                #print(bleu_loss.diag())

            elif args.cross_entropy:
                beam_scores = F.softmax(beam_scores, dim=-1)
                ce_result = []

                for k in range(args.beam_size):
                    # best_beam_ids[j]   total_eos_probs[k, j] *
                    # ce_result.append(nll_loss(sample_logp[k].contiguous().view(-1, probs.size(3)), \
                    #                             tgt_sents_var[1:].contiguous().view(-1)))
                    ce_result.append(
                        torch.stack([nll_loss(sample_logp[k][j].contiguous().view(-1, probs.size(3)), \
                                                tgt_sents_var[1:,j].contiguous().view(-1)) for j in range(batch_size)]))

                # ce_stacked = torch.stack([cross_entropy_loss(probs[k].contiguous().view(-1, scores.size(3)),\
                #                                              tgt_sents_var[1:].contiguous().view(-1)) for k in range(args.beam_size)]).unsqueeze(1)
                ce_stacked = torch.stack(ce_result)

                bleu_loss = torch.trace(torch.mm(beam_scores, ce_stacked)) / (batch_size)
                logger.add_scalar(train_iter, 'ce', bleu_loss.item())
            else:
                print("corpus bleu")

                bleu_scores = torch.stack([bleu(probs[k], r,\
                            torch.tensor(hypo_lengths[k].tolist(),dtype=torch.long, device=device),\
                             ref_lengths.tolist(), smooth=True,device=device)[0] for k in range(args.beam_size)], dim=0)
                #bleu_loss = bleu_scores[0]

                bleu_loss = F.softmax(torch.sum(beam_scores,dim=0),dim=-1).dot(bleu_scores)


            #word_loss = cross_entropy_loss(scores.view(-1, scores.size(2)), tgt_sents_var[1:].view(-1))
            # loss = word_loss / batch_size
            loss = bleu_loss
            #sample_logp = F.log_softmax(scores, dim=-1)
            with torch.no_grad():
                entropy = - (probs * sample_logp).sum() / (batch_size * args.beam_size * sample_logp.size(2))

            logger.add_scalar(train_iter, 'entropy', entropy.item())
            # exit()
            # word_loss_val = word_loss.data[0]
            # loss_val = loss.data[0]

            word_loss_val = bleu_loss.item()
            loss_val = loss.item()

            # a = list(model.parameters())[0].clone()
            loss.backward()
            # clip gradient
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad)
            optimizer.step()

            logger.add_scalar(train_iter,'total_bleu_loss', bleu_loss.item())
            #grads = list(model.parameters())[0].grad
            #print(grads)
            #print(loss.grad)
            # b = list(model.parameters())[0].clone()
            # print(torch.equal(a.data, b.data))
            # exit()
            report_loss += entropy
            cum_loss += word_loss_val
            report_tgt_words += pred_tgt_word_num
            cum_tgt_words += pred_tgt_word_num
            report_examples += batch_size
            cum_examples += batch_size
            cum_batches += batch_size
            logger.save()
            if train_iter % args.log_every == 0:
                logger.iter_info()
                print('loss', loss_val)
                print('bleu loss', bleu_loss.item())
                print('epoch %d, iter %d, avg. loss %.2f, avg. ppl %.2f ' \
                      'cum. examples %d, speed %.2f words/sec, time elapsed %.2f sec' % (epoch, train_iter,
                                                                                         report_loss / report_examples,
                                                                                         np.exp(report_loss / report_tgt_words),
                                                                                         cum_examples,
                                                                                         report_tgt_words / (time.time() - train_time),
                                                                                         time.time() - begin_time), file=sys.stderr)

                train_time = time.time()
                report_loss = report_tgt_words = report_examples = 0.
            # perform validation
            if train_iter % args.valid_niter == 0:
                print('epoch %d, iter %d, cum. loss %.2f, cum. ppl %.2f cum. examples %d' % (epoch, train_iter,
                                                                                         cum_loss / cum_batches,
                                                                                         np.exp(cum_loss / cum_tgt_words),
                                                                                         cum_examples), file=sys.stderr)

                cum_loss = cum_batches = cum_tgt_words = 0.
                valid_num += 1

                print('begin validation ...', file=sys.stderr)
                model.eval()

                # compute dev. ppl and bleu
                dev_loss = evaluate_loss(model, dev_data, cross_entropy_loss)
                dev_ppl = np.exp(dev_loss)

                if args.valid_metric in ['bleu', 'word_acc', 'sent_acc']:
                    dev_hyps, dev_tgt = decode(model, dev_data, verbose=False, batch_size=1)
                    dev_hyps = [hyps[0] for hyps in dev_hyps]

                    #
                    # dev_hyps2, dev_tgt2 = decode(model, dev_data, verbose=False, beam_size=args.beam_size-1)
                    # dev_hyps2 = [hyps[0] for hyps in dev_hyps2]
                    # valid_metric = compute_bleu([[tgt] for tgt in dev_tgt2], dev_hyps2)[0]
                    # print("Fake bleu:", valid_metric)

                    if args.valid_metric == 'bleu':
                        # valid_metric = get_bleu([tgt for src, tgt in dev_data], dev_hyps)
                        valid_metric = compute_bleu([[tgt] for tgt in dev_tgt], dev_hyps)[0]
                        sentence_level = [sentence_bleu([tgt], dev) for tgt,dev in zip(dev_tgt, dev_hyps)]
                        print('-'*10)
                        print('Corpus bleu', valid_metric)
                        print('Sentence bleu',sum(sentence_level)/len(dev_tgt) )
                        print('-'*10)
                    else:
                        valid_metric = get_acc([tgt for src, tgt in dev_data], dev_hyps, acc_type=args.valid_metric)
                    print('validation: iter %d, dev. ppl %f, dev. %s %f' % (train_iter, dev_ppl, args.valid_metric, valid_metric),
                          file=sys.stderr)
                else:
                    valid_metric = -dev_ppl
                    print('validation: iter %d, dev. ppl %f' % (train_iter, dev_ppl),
                          file=sys.stderr)

                model.train()
                print(hist_valid_scores)
                is_better = len(hist_valid_scores) == 0 or valid_metric > max(hist_valid_scores)
                is_better_than_last = len(hist_valid_scores) == 0 or valid_metric > hist_valid_scores[-1]
                hist_valid_scores.append(valid_metric)
                if valid_num > args.save_model_after:
                    model_file = args.save_to
                    print(args.save_to)
                    if is_better:
                        print('save model to [%s]' % model_file, file=sys.stderr)
                        model.save(model_file)
                    try:
                        model.save("./models/model_relax_beam{}_{}".format(args.beam_size,train_iter))
                    except Exception:
                        pass


                # if (not is_better_than_last) and args.lr_decay:
                #     lr = optimizer.param_groups[0]['lr'] * args.lr_decay
                #     print('decay learning rate to %f' % lr, file=sys.stderr)
                #     optimizer.param_groups[0]['lr'] = lr

                if is_better:
                    patience = 0
                    best_model_iter = train_iter

                    if valid_num > args.save_model_after:
                        print('save currently the best model ..', file=sys.stderr)
                        model_file_abs_path = os.path.abspath(model_file)
                        symlin_file_abs_path = os.path.abspath(args.save_to + '.bin')
                        os.system('ln -sf %s %s' % (model_file_abs_path, symlin_file_abs_path))
                else:
                    patience += 1
                    print('hit patience %d' % patience, file=sys.stderr)
                    if patience == args.patience:
                        print('early stop!', file=sys.stderr)
                        print('the best model is from iteration [%d]' % best_model_iter, file=sys.stderr)
                        exit(0)
            tqdm_bar.update(batch_size)
        tqdm_bar.close()


def custom_train2(args):
    """
    Training bleu with brevity penalty LB
    """
    train_data_src = read_corpus(args.train_src, source='src')
    train_data_tgt = read_corpus(args.train_tgt, source='tgt')

    dev_data_src = read_corpus(args.dev_src, source='src')
    dev_data_tgt = read_corpus(args.dev_tgt, source='tgt')

    train_data = list(zip(train_data_src, train_data_tgt))[:128]
    dev_data = list(zip(dev_data_src, dev_data_tgt))


    vocab, model, optimizer, nll_loss, cross_entropy_loss = init_custom_train(args)

    train_iter = patience = cum_loss = report_loss = cum_tgt_words = report_tgt_words = 0
    cum_examples = cum_batches = report_examples = epoch = valid_num = best_model_iter = 0
    hist_valid_scores = []
    train_time = begin_time = time.time()
    print('begin custom training')

    while True:
        epoch += 1
        if epoch > args.max_epoch:
            exit(0)
        print('current epoch', epoch)
        di = data_iter(train_data, batch_size=args.batch_size)
        #train_data = list(zip(train_data_src, train_data_tgt))

        tqdm_bar = tqdm(total=len(train_data))

        for src_sents, tgt_sents in di:
            # print(epoch, '--', train_iter)
            train_iter += 1

            src_sents_var = to_input_variable(src_sents, vocab.src, cuda=args.cuda)
            tgt_sents_var = to_input_variable(tgt_sents, vocab.tgt, cuda=args.cuda)

            batch_size = len(src_sents)
            src_sents_len = [len(s) for s in src_sents]
            pred_tgt_word_num = sum(len(s[1:]) for s in tgt_sents) # omitting leading `<s>`

            optimizer.zero_grad()

            # (tgt_sent_len, batch_size, tgt_vocab_size)
            scores = model(src_sents_var, src_sents_len, tgt_sents_var[:-1])
            scores_numpy = scores.data.cpu().numpy()
            tgt_sents_numpy = tgt_sents_var.data.cpu().numpy()
            eos = model.vocab.tgt['</s>']
            bos = model.vocab.tgt['<s>']
            def _find_lentghs(sent):
                """ sent (sent_len x batch_size) """
                tmp = sent == eos
                return np.argmax(tmp, axis=0) + 1

            greedy_hypo = np.argmax(scores_numpy, axis=2)
            hypo_lengths = _find_lentghs(greedy_hypo) # no bos
            ref_lengths = _find_lentghs(tgt_sents_numpy) - 1 #because of bos
            regular_sm = MySoftmax()
            probs = regular_sm(scores.permute(1, 0, 2))#[:, :-1 , :]
            refs = tgt_sents_var.permute(1, 0)[:, 1:]
            r = refs.data.cpu().numpy().tolist()
            bleu_loss, _ = bleu_with_bp(probs, r,\
                                        ref_lengths.tolist(), eos, smooth=True)
            loss = bleu_loss
            word_loss_val = bleu_loss.item()
            loss_val = bleu_loss.item()

            loss.backward()
            # clip gradient
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad)
            optimizer.step()

            report_loss += word_loss_val
            cum_loss += word_loss_val
            report_tgt_words += pred_tgt_word_num
            cum_tgt_words += pred_tgt_word_num
            report_examples += batch_size
            cum_examples += batch_size
            cum_batches += batch_size

            tqdm_bar.update(batch_size)

            if train_iter % args.log_every == 0:
                print('bleu loss', bleu_loss.item())
                print('epoch %d, iter %d, avg. loss %.2f, avg. ppl %.2f ' \
                      'cum. examples %d, speed %.2f words/sec, time elapsed %.2f sec' % (epoch, train_iter,
                                                                                         report_loss / report_examples,
                                                                                         np.exp(report_loss / report_tgt_words),
                                                                                         cum_examples,
                                                                                         report_tgt_words / (time.time() - train_time),
                                                                                         time.time() - begin_time), file=sys.stderr)

                train_time = time.time()
                report_loss = report_tgt_words = report_examples = 0.
            # perform validation
            if train_iter % args.valid_niter == 0:
                print('epoch %d, iter %d, cum. loss %.2f, cum. ppl %.2f cum. examples %d' % (epoch, train_iter,
                                                                                         cum_loss / cum_batches,
                                                                                         np.exp(cum_loss / cum_tgt_words),
                                                                                         cum_examples), file=sys.stderr)

                cum_loss = cum_batches = cum_tgt_words = 0.
                valid_num += 1

                print('begin validation ...', file=sys.stderr)
                model.eval()

                # compute dev. ppl and bleu

                dev_loss = evaluate_loss(model, dev_data, cross_entropy_loss)
                dev_ppl = np.exp(dev_loss)

                if args.valid_metric in ['bleu', 'word_acc', 'sent_acc']:
                    dev_hyps, dev_tgt = decode(model, dev_data, verbose=False)
                    dev_hyps = [hyps[0] for hyps in dev_hyps]
                    if args.valid_metric == 'bleu':
                        # valid_metric = get_bleu([tgt for src, tgt in dev_data], dev_hyps)
                        valid_metric = compute_bleu([[tgt] for tgt in dev_tgt], dev_hyps)[0]
                        # print('-'*10)
                        # print('compute_bleu', valid_metric)
                        # print('-'*10)
                    else:
                        valid_metric = get_acc([tgt for src, tgt in dev_data], dev_hyps, acc_type=args.valid_metric)
                    print('validation: iter %d, dev. ppl %f, dev. %s %f' % (train_iter, dev_ppl, args.valid_metric, valid_metric),
                          file=sys.stderr)
                else:
                    valid_metric = -dev_ppl
                    print('validation: iter %d, dev. ppl %f' % (train_iter, dev_ppl),
                          file=sys.stderr)

                model.train()

                is_better = len(hist_valid_scores) == 0 or valid_metric > max(hist_valid_scores)
                is_better_than_last = len(hist_valid_scores) == 0 or valid_metric > hist_valid_scores[-1]
                hist_valid_scores.append(valid_metric)
                if valid_num > args.save_model_after:
                    model_file = args.save_to + '.iter%d.bin' % train_iter
                    print('save model to [%s]' % model_file, file=sys.stderr)
                    model.save(model_file)

                if (not is_better_than_last) and args.lr_decay:
                    lr = optimizer.param_groups[0]['lr'] * args.lr_decay
                    print('decay learning rate to %f' % lr, file=sys.stderr)
                    optimizer.param_groups[0]['lr'] = lr

                if is_better:
                    patience = 0
                    best_model_iter = train_iter

                    if valid_num > args.save_model_after:
                        print('save currently the best model ..', file=sys.stderr)
                        model_file_abs_path = os.path.abspath(model_file)
                        symlin_file_abs_path = os.path.abspath(args.save_to + '.bin')
                        os.system('ln -sf %s %s' % (model_file_abs_path, symlin_file_abs_path))
                else:
                    patience += 1
                    print('hit patience %d' % patience, file=sys.stderr)
                    if patience == args.patience:
                        print('early stop!', file=sys.stderr)
                        print('the best model is from iteration [%d]' % best_model_iter, file=sys.stderr)
                        exit(0)
        tqdm_bar.close()


def custom_train3(args):
    """
    Training with reinforce algorithm
    """
    train_data_src = read_corpus(args.train_src, source='src')
    train_data_tgt = read_corpus(args.train_tgt, source='tgt')

    dev_data_src = read_corpus(args.dev_src, source='src')
    dev_data_tgt = read_corpus(args.dev_tgt, source='tgt')

    train_data = list(zip(train_data_src, train_data_tgt))
    dev_data = list(zip(dev_data_src, dev_data_tgt))


    vocab, model, optimizer, nll_loss, cross_entropy_loss = init_custom_train(args)

    train_iter = patience = cum_loss = report_loss = cum_tgt_words = report_tgt_words = 0
    cum_examples = cum_batches = report_examples = epoch = valid_num = best_model_iter = 0
    hist_valid_scores = []
    train_time = begin_time = time.time()
    print('begin custom training')
    fmt = {'bleu_loss': '.5e'}
    logger = Logger("reinforce_train", fmt=fmt)
    while True:
        epoch += 1
        if epoch > args.max_epoch:
            exit(0)
        print('current epoch', epoch)
        di = data_iter(train_data, batch_size=args.batch_size)
        train_data = list(zip(train_data_src, train_data_tgt))

        tqdm_bar = tqdm(total=len(train_data))
        loss= None
        for src_sents, tgt_sents in di:
            # print(epoch, '--', train_iter)
            train_iter += 1

            src_sents_var = to_input_variable(src_sents, vocab.src)
            tgt_sents_var = to_input_variable(tgt_sents, vocab.tgt)

            batch_size = len(src_sents)
            src_sents_len = [len(s) for s in src_sents]
            pred_tgt_word_num = sum(len(s[1:]) for s in tgt_sents) # omitting leading `<s>`

            optimizer.zero_grad()

            # (tgt_sent_len, batch_size, tgt_vocab_size)
            scores = model(src_sents_var, src_sents_len, tgt_sents_var[:-1], args.use_teacher_forcing)
            eos = model.vocab.tgt['</s>']
            bos = model.vocab.tgt['<s>']

            refs = tgt_sents_var[1:].transpose(0, 1)  # remove bos

            # REINFORCE part
            r = refs.data.cpu().numpy().tolist()
            probs = F.softmax(scores.permute(1, 0, 2), dim=2)
            del scores

            multi_samples = torch.distributions.Categorical(probs)
            sample = multi_samples.sample().to(device)
            greedy_sample = torch.max(probs, dim=2)[1]
            sample_bleu = bleu_score(sample, r, model.vocab.tgt.id2word, corpus_average=False)
            greedy_bleu = bleu_score(greedy_sample, r, model.vocab.tgt.id2word, corpus_average=False)
            advantage = sample_bleu - greedy_bleu
            advantage = torch.tensor(advantage, dtype=torch.float, device=device)
            mask = infer_mask(sample, eos)

            J = multi_samples.log_prob(sample) * advantage[:, None] *mask
            # average with mask
            loss = - torch.sum(J ) / torch.sum(mask)
            word_loss_val = loss.item()
            loss_val = loss.item()
            logger.add_scalar(train_iter, 'loss', loss.item())

            loss.backward()
            # clip gradient
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad)
            optimizer.step()

            logger.save()
            report_loss += word_loss_val
            cum_loss += word_loss_val
            report_tgt_words += pred_tgt_word_num
            cum_tgt_words += pred_tgt_word_num
            report_examples += batch_size
            cum_examples += batch_size
            cum_batches += batch_size
            if train_iter % args.log_every == 0:
                print('epoch %d, iter %d, avg. loss %.2f, avg. ppl %.2f ' \
                      'cum. examples %d, speed %.2f words/sec, time elapsed %.2f sec' % (epoch, train_iter,
                                                                                         report_loss / report_examples,
                                                                                         np.exp(report_loss / report_tgt_words),
                                                                                         cum_examples,
                                                                                         report_tgt_words / (time.time() - train_time),
                                                                                         time.time() - begin_time), file=sys.stderr)

                train_time = time.time()
                report_loss = report_tgt_words = report_examples = 0.
            # perform validation
            if train_iter % args.valid_niter == 0:
                print('epoch %d, iter %d, cum. loss %.2f, cum. ppl %.2f cum. examples %d' % (epoch, train_iter,
                                                                                         cum_loss / cum_batches,
                                                                                         np.exp(cum_loss / cum_tgt_words),
                                                                                         cum_examples), file=sys.stderr)

                cum_loss = cum_batches = cum_tgt_words = 0.
                valid_num += 1

                print('begin validation ...', file=sys.stderr)
                model.eval()

                # compute dev. ppl and bleu

                dev_loss = evaluate_loss(model, dev_data, cross_entropy_loss)
                dev_ppl = np.exp(dev_loss)

                if args.valid_metric in ['bleu', 'word_acc', 'sent_acc']:
                    dev_hyps,dev_tgt = decode(model, dev_data, verbose=False)
                    dev_hyps = [hyps[0] for hyps in dev_hyps]
                    if args.valid_metric == 'bleu':
                        # valid_metric = get_bleu([tgt for src, tgt in dev_data], dev_hyps)
                        valid_metric = compute_bleu([[tgt] for tgt in dev_tgt], dev_hyps)[0]
                        # print('-'*10)
                        # print('compute_bleu', valid_metric)
                        # print('-'*10)
                    else:
                        valid_metric = get_acc([tgt for src, tgt in dev_data], dev_hyps, acc_type=args.valid_metric)
                    print('validation: iter %d, dev. ppl %f, dev. %s %f' % (train_iter, dev_ppl, args.valid_metric, valid_metric),
                          file=sys.stderr)
                else:
                    valid_metric = -dev_ppl
                    print('validation: iter %d, dev. ppl %f' % (train_iter, dev_ppl),
                          file=sys.stderr)

                model.train()

                is_better = len(hist_valid_scores) == 0 or valid_metric > max(hist_valid_scores)
                is_better_than_last = len(hist_valid_scores) == 0 or valid_metric > hist_valid_scores[-1]
                hist_valid_scores.append(valid_metric)
                if valid_num > args.save_model_after:
                    model_file = args.save_to + '.iter%d.bin' % train_iter
                    print('save model to [%s]' % model_file, file=sys.stderr)
                    model.save(model_file)

                if (not is_better_than_last) and args.lr_decay:
                    lr = optimizer.param_groups[0]['lr'] * args.lr_decay
                    print('decay learning rate to %f' % lr, file=sys.stderr)
                    optimizer.param_groups[0]['lr'] = lr

                if is_better:
                    patience = 0
                    best_model_iter = train_iter

                    if valid_num > args.save_model_after:
                        print('save currently the best model ..', file=sys.stderr)
                        model_file_abs_path = os.path.abspath(model_file)
                        symlin_file_abs_path = os.path.abspath(args.save_to + '.bin')
                        os.system('ln -sf %s %s' % (model_file_abs_path, symlin_file_abs_path))
                else:
                    patience += 1
                    print('hit patience %d' % patience, file=sys.stderr)
                    if patience == args.patience:
                        print('early stop!', file=sys.stderr)
                        print('the best model is from iteration [%d]' % best_model_iter, file=sys.stderr)
                        exit(0)
            tqdm_bar.update(batch_size)
        tqdm_bar.close()


def train(args):
    print("Device {} available".format(device))

    train_data_src = read_corpus(args.train_src, source='src')
    train_data_tgt = read_corpus(args.train_tgt, source='tgt')

    dev_data_src = read_corpus(args.dev_src, source='src')
    dev_data_tgt = read_corpus(args.dev_tgt, source='tgt')

    train_data = list(zip(train_data_src, train_data_tgt))
    dev_data = list(zip(dev_data_src, dev_data_tgt))

    vocab, model, optimizer, nll_loss, cross_entropy_loss = init_custom_train(args)

    train_iter = patience = cum_loss = report_loss = cum_tgt_words = report_tgt_words = 0
    cum_examples = cum_batches = report_examples = epoch = valid_num = best_model_iter = 0
    hist_valid_scores = []
    train_time = begin_time = time.time()
    print('begin Maximum Likelihood training')

    while True:
        epoch += 1
        print('current epoch', epoch)
        di = data_iter(train_data, batch_size=args.batch_size)
        #train_data = zip(train_data_src, train_data_tgt)

        tqdm_bar = tqdm(total=len(train_data))

        for src_sents, tgt_sents in di:
            # print(epoch, '--', train_iter)
            train_iter += 1

            src_sents_var = to_input_variable(src_sents, vocab.src)
            tgt_sents_var = to_input_variable(tgt_sents, vocab.tgt)
            batch_size = len(src_sents)
            src_sents_len = [len(s) for s in src_sents]
            pred_tgt_word_num = sum(len(s[1:]) for s in tgt_sents) # omitting leading `<s>`
            assert src_sents_var.size()[1] == batch_size

            optimizer.zero_grad()
            # (tgt_sent_len, batch_size, tgt_vocab_size)
            scores = model(src_sents_var, src_sents_len, tgt_sents_var[:-1], args.use_teacher_forcing)
            #scores.size()[1] == batch_size == tgt_sents_var.size()[1]:
            word_loss = cross_entropy_loss(scores.view(-1, scores.size(2)), tgt_sents_var[1:].contiguous().view(-1))
            loss = word_loss / batch_size
            word_loss_val = word_loss.item()
            loss_val = loss.item()

            # a = list(model.parameters())[0].clone()

            # word_loss_val = word_loss.data[0]
            # loss_val = loss.data[0]
            word_loss.backward()

            # clip gradient
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad)
            optimizer.step()


            # grads = list(model.parameters())[0].grad
            # print(grads)
            # print(loss.grad)
            # b = list(model.parameters())[0].clone()

            # print(torch.equal(a,b))
            report_loss += word_loss_val
            cum_loss += word_loss_val
            report_tgt_words += pred_tgt_word_num
            cum_tgt_words += pred_tgt_word_num
            report_examples += batch_size
            cum_examples += batch_size
            cum_batches += batch_size

            if train_iter % args.log_every == 0:
                print('current learnin rage %.10f' % (optimizer.state_dict()['param_groups'][0]['lr']))
                print('epoch %d, iter %d, avg. loss %.2f, avg. ppl %.2f ' \
                      'cum. examples %d, speed %.2f words/sec, time elapsed %.2f sec' % (epoch, train_iter,
                                                                                         report_loss / report_examples,
                                                                                         np.exp(report_loss / report_tgt_words),
                                                                                         cum_examples,
                                                                                         report_tgt_words / (time.time() - train_time),
                                                                                         time.time() - begin_time), file=sys.stderr)

                train_time = time.time()
                report_loss = report_tgt_words = report_examples = 0.

            # perform validation
            # print('hist_valid_scores')
            # print(hist_valid_scores)
            if train_iter % args.valid_niter == 0:
                print('epoch %d, iter %d, cum. loss %.2f, cum. ppl %.2f cum. examples %d' % (epoch, train_iter,
                                                                                         cum_loss / cum_batches,
                                                                                         np.exp(cum_loss / cum_tgt_words),
                                                                                         cum_examples), file=sys.stderr)

                cum_loss = cum_batches = cum_tgt_words = 0.
                valid_num += 1

                print('begin validation ...', file=sys.stderr)
                model.eval()

                # compute dev. ppl and bleu

                dev_loss = evaluate_loss(model, dev_data, cross_entropy_loss)
                dev_ppl = np.exp(dev_loss)

                if args.valid_metric in ['bleu', 'word_acc', 'sent_acc']:
                    dev_hyps, dev_tgt = decode(model, dev_data, batch_size=32, verbose=False)
                    dev_hyps = [hyps[0] for hyps in dev_hyps]
                    if args.valid_metric == 'bleu':
                        # valid_metric = get_bleu([tgt for src, tgt in dev_data], dev_hyps)
                        # print('-' * 10)
                        # print('[tgt for src, tgt in dev_data]')
                        # print([tgt for src, tgt in dev_data])
                        # print('dev_hyps')
                        # print(dev_hyps)
                        # print('-' * 10)
                        valid_metric = compute_bleu([[tgt] for tgt in dev_tgt], dev_hyps)[0]
                        # print('-'*10)
                        # print('compute_bleu', valid_metric)
                        # print('-'*10)
                    else:
                        valid_metric = get_acc([tgt for src, tgt in dev_data], dev_hyps, acc_type=args.valid_metric)

                    print('validation: iter %d, dev. ppl %f, dev. %s %f' % (train_iter, dev_ppl, args.valid_metric, valid_metric),
                          file=sys.stderr)
                else:
                    valid_metric = -dev_ppl
                    print('validation: iter %d, dev. ppl %f' % (train_iter, dev_ppl),
                          file=sys.stderr)

                model.train()

                is_better = len(hist_valid_scores) == 0 or valid_metric > max(hist_valid_scores)
                print('-' * 10)
                print('checking if better')
                print(valid_metric)
                print('vs')
                if len(hist_valid_scores) > 0:
                    print(max(hist_valid_scores))
                else:
                    print('fist step')
                print(hist_valid_scores)
                print('-' * 10)
                is_better_than_last = len(hist_valid_scores) == 0 or valid_metric > hist_valid_scores[-1]
                hist_valid_scores.append(valid_metric)

                if valid_num > args.save_model_after:
                    model_file = args.save_to + '.iter%d.bin' % train_iter
                    print('save model to [%s]' % model_file, file=sys.stderr)
                    model.save(model_file)

                if (not is_better_than_last) and args.lr_decay:
                    lr = optimizer.param_groups[0]['lr'] * args.lr_decay
                    print('decay learning rate to %f' % lr, file=sys.stderr)
                    optimizer.param_groups[0]['lr'] = lr

                if is_better:
                    patience = 0
                    best_model_iter = train_iter

                    if valid_num > args.save_model_after:
                        print('save currently the best model ..', file=sys.stderr)
                        model_file_abs_path = os.path.abspath(model_file)
                        symlin_file_abs_path = os.path.abspath(args.save_to + '.bin')
                        os.system('ln -sf %s %s' % (model_file_abs_path, symlin_file_abs_path))
                else:
                    patience += 1
                    print('hit patience %d' % patience, file=sys.stderr)
                    if patience == args.patience:
                        print('early stop!', file=sys.stderr)
                        print('the best model is from iteration [%d]' % best_model_iter, file=sys.stderr)
                        exit(0)
            tqdm_bar.update(batch_size)
        tqdm_bar.close()


def train_with_sampling(args):
    """
    Training with reinforce algorithm and sampling
    """

    train_data_src = read_corpus(args.train_src, source='src')
    train_data_tgt = read_corpus(args.train_tgt, source='tgt')

    dev_data_src = read_corpus(args.dev_src, source='src')
    dev_data_tgt = read_corpus(args.dev_tgt, source='tgt')

    train_data = list(zip(train_data_src, train_data_tgt))
    dev_data = list(zip(dev_data_src, dev_data_tgt))

    vocab, model, optimizer, nll_loss, cross_entropy_loss = init_custom_train(args)

    train_iter = patience = cum_loss = report_loss = cum_tgt_words = report_tgt_words = 0
    cum_examples = cum_batches = report_examples = epoch = valid_num = best_model_iter = 0
    hist_valid_scores = []
    train_time = begin_time = time.time()
    print('begin training with sampling')
    print("Expected number of epoch{}".format(args.max_epoch))
    while True:
        epoch += 1
        if epoch > args.max_epoch:
            exit(0)
        print('current epoch', epoch)
        #di = batch_slice(train_data, batch_size=64)
        di = data_iter(train_data, batch_size=64)
        tqdm_bar = tqdm(total=len(train_data))

        for src_sents, tgt_sents in di:

            # print(epoch, '--', train_iter)
            train_iter += 1

            src_sents_var = to_input_variable(src_sents, vocab.src)#.permute(1,0)
            tgt_sents_var = to_input_variable(tgt_sents, vocab.tgt)#.permute(1,0)
            batch_size = len(src_sents)
            src_sents_len = [len(s) for s in src_sents]
            tgt_sents_len = [len(t) for t in tgt_sents]
            pred_tgt_word_num = sum(len(s[1:]) for s in tgt_sents) # omitting leading `<s>`

            optimizer.zero_grad()
            model.zero_grad()
            # (tgt_sent_len, batch_size, tgt_vocab_size)
            scores = model(src_sents_var, src_sents_len, tgt_sents_var, use_teacher_forcing=False)

            eos = model.vocab.tgt['</s>']
            bos = model.vocab.tgt['<s>']
            refs = tgt_sents_var[1:].transpose(0,1)#  remove bos

            #REINFORCE part
            r = refs.data.cpu().numpy().tolist()
            probs = F.softmax(scores.permute(1,0,2),dim=2)
            del scores

            multi_samples = torch.distributions.Categorical(probs)
            sample = multi_samples.sample()
            greedy_sample = torch.max(probs, dim=2)[1]
            sample_bleu = bleu_score(sample, r, model.vocab.tgt.id2word, corpus_average=False)
            greedy_bleu = bleu_score(greedy_sample, r, model.vocab.tgt.id2word, corpus_average=False)
            advantage = sample_bleu - greedy_bleu
            advantage = torch.tensor(advantage, dtype=torch.float, device=device)
            J = torch.sum(multi_samples.log_prob(sample) * advantage[:,None])
            # average with mask

            mask = infer_mask(sample, eos)
            loss = - torch.sum(J * mask) / torch.sum(mask)
            #TODO add regularizer with entropy

            word_loss_val = loss.item()

            if args.debug:
                #GPU PROFILE
                gpu_mem_dump()

            loss_val = loss.item()
            loss.backward()
            # clip gradient
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad)
            optimizer.step()

            report_loss += word_loss_val
            cum_loss += word_loss_val
            report_tgt_words += pred_tgt_word_num
            cum_tgt_words += pred_tgt_word_num
            report_examples += batch_size
            cum_examples += batch_size
            cum_batches += batch_size

            if train_iter % args.log_every == 0:
                print('loss', loss_val)
                print('epoch %d, iter %d, avg. loss %.2f, avg. ppl %.2f ' \
                      'cum. examples %d, speed %.2f words/sec, time elapsed %.2f sec' % (epoch, train_iter,
                                                                                         report_loss / report_examples,
                                                                                         np.exp(report_loss / report_tgt_words),
                                                                                         cum_examples,
                                                                                         report_tgt_words / (time.time() - train_time),
                                                                                         time.time() - begin_time), file=sys.stderr)

                train_time = time.time()
                report_loss = report_tgt_words = report_examples = 0.
            # perform validation
            if train_iter % args.valid_niter == 0:

                print('epoch %d, iter %d, cum. loss %.2f, cum. ppl %.2f cum. examples %d' % (epoch, train_iter,
                                                                                         cum_loss / cum_batches,
                                                                                         np.exp(cum_loss / cum_tgt_words),
                                                                                         cum_examples), file=sys.stderr)

                cum_loss = cum_batches = cum_tgt_words = 0.
                valid_num += 1

                print('begin validation ...', file=sys.stderr)
                model.eval()

                # compute dev. ppl and bleu
                print("Validation data:",len(dev_data))
                dev_loss = evaluate_loss(model, dev_data, cross_entropy_loss, use_teacher_forcing=True)
                dev_ppl = np.exp(dev_loss)

                if args.valid_metric in ['bleu', 'word_acc', 'sent_acc']:
                    dev_hyps, dev_tgt = decode(model, dev_data, verbose=False)
                    dev_hyps = [hyps[0] for hyps in dev_hyps]
                    if args.valid_metric == 'bleu':
                        # valid_metric = get_bleu([tgt for src, tgt in dev_data], dev_hyps)
                        valid_metric = compute_bleu([[tgt] for tgt in dev_tgt], dev_hyps)[0]
                        print('-'*10)
                        print('computed_bleu', valid_metric)
                        print('-'*10)
                    else:
                        valid_metric = get_acc([tgt for src, tgt in dev_data], dev_hyps, acc_type=args.valid_metric)
                    print('validation: iter %d, dev. ppl %f, dev. %s %f' % (train_iter, dev_ppl, args.valid_metric, valid_metric),
                          file=sys.stderr)
                else:
                    valid_metric = -dev_ppl
                    print('validation: iter %d, dev. ppl %f' % (train_iter, dev_ppl),
                          file=sys.stderr)

                model.train()

                is_better = len(hist_valid_scores) == 0 or valid_metric > max(hist_valid_scores)
                is_better_than_last = len(hist_valid_scores) == 0 or valid_metric > hist_valid_scores[-1]
                hist_valid_scores.append(valid_metric)
                if valid_num > args.save_model_after:
                    model_file = args.save_to + '.iter%d_reinforce_sampling.bin' % epoch
                    print('save model to [%s]' % model_file, file=sys.stderr)
                    model.save(model_file)

                if (not is_better_than_last) and args.lr_decay:
                    lr = optimizer.param_groups[0]['lr'] * args.lr_decay
                    print('decay learning rate to %f' % lr, file=sys.stderr)
                    optimizer.param_groups[0]['lr'] = lr

                if is_better:
                    patience = 0
                    best_model_iter = train_iter

                    if valid_num > args.save_model_after:
                        print('save currently the best model ..', file=sys.stderr)
                        model_file_abs_path = os.path.abspath(model_file)
                        symlin_file_abs_path = os.path.abspath(args.save_to + '.bin')
                        os.system('ln -sf %s %s' % (model_file_abs_path, symlin_file_abs_path))
                else:
                    patience += 1
                    print('hit patience %d' % patience, file=sys.stderr)
                    if patience == args.patience:
                        print('early stop!', file=sys.stderr)
                        print('the best model is from iteration [%d]' % best_model_iter, file=sys.stderr)
                        exit(0)
            tqdm_bar.update(batch_size)
        tqdm_bar.close()


def train_actor(args):
    """
    Training actor for diversity decoding
    """

    train_data_src = read_corpus(args.train_src, source='src')
    train_data_tgt = read_corpus(args.train_tgt, source='tgt')

    dev_data_src = read_corpus(args.dev_src, source='src')
    dev_data_tgt = read_corpus(args.dev_tgt, source='tgt')

    train_data = list(zip(train_data_src, train_data_tgt))
    dev_data = list(zip(dev_data_src, dev_data_tgt))

    vocab, model, optimizer, nll_loss, cross_entropy_loss = init_custom_train(args)

    train_iter = patience = cum_loss = report_loss = cum_tgt_words = report_tgt_words = 0
    cum_examples = cum_batches = report_examples = epoch = valid_num = best_model_iter = 0
    hist_valid_scores = []
    train_time = begin_time = time.time()
    print('begin actor training')
    print("Expected number of epoch{}".format(args.max_epoch))
    # Actor take vector with shape (att+hidden )
    #with torch.enable_grad():
    # for i in model.parameters():
    #     i.requires_grad = False
    #model.actor = Actor(2 * args.hidden_size, 64, 2 * args.hidden_size).to(device)
    #     p.data.uniform_(-args.uniform_init, args.uniform_init)
    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])
    print("The number of trainable parameters:", params)
    while True:
        epoch += 1
        if epoch > args.max_epoch:
            exit(0)
        print('current epoch', epoch)
        di = data_iter(train_data, batch_size=64)
        tqdm_bar = tqdm(total=len(train_data))
        for src_sents, tgt_sents in di:

            train_iter += 1
            src_sents_var = to_input_variable(src_sents, vocab.src)
            tgt_sents_var = to_input_variable(tgt_sents, vocab.tgt)

            batch_size = len(src_sents)
            src_sents_len = [len(s) for s in src_sents]
            pred_tgt_word_num = sum(len(s[1:]) for s in tgt_sents) # omitting leading `<s>`

            optimizer.zero_grad()

            # (tgt_sent_len, batch_size, tgt_vocab_size)
            scores = model(src_sents_var, src_sents_len, tgt_sents_var[:-1], use_teacher_forcing=False, use_actor=True)
            scores_numpy = scores.data.cpu().numpy()
            tgt_sents_numpy = tgt_sents_var.data.cpu().numpy()
            eos = model.vocab.tgt['</s>']
            bos = model.vocab.tgt['<s>']
            def _find_lentghs(sent):
                """ sent (sent_len x batch_size) """
                tmp = sent == eos
                return np.argmax(tmp, axis=0) + 1

            greedy_hypo = np.argmax(scores_numpy, axis=2)
            hypo_lengths = _find_lentghs(greedy_hypo) # no bos
            ref_lengths = _find_lentghs(tgt_sents_numpy) - 1 #because of bos

            regular_sm = MySoftmax()
            probs = regular_sm(scores.permute(1, 0, 2))#[batch_size, sents_length , target_vocab]
            refs = tgt_sents_var[1:].permute(1, 0)
            r = refs.data.cpu().numpy().tolist()

            bleu_loss, _ = bleu(probs, r,\
                            torch.tensor(hypo_lengths.tolist(),dtype=torch.long, device=device),\
                            ref_lengths.tolist(), smooth=True,device=device)

            loss = bleu_loss

            word_loss_val = loss.item()

            if args.debug:
                #GPU PROFILE
                gpu_mem_dump()

            loss_val = loss.item()
            loss.backward()
            # clip gradient
            torch.nn.utils.clip_grad_norm_(model.actor.parameters(), args.clip_grad)
            optimizer.step()

            report_loss += word_loss_val
            cum_loss += word_loss_val
            report_tgt_words += pred_tgt_word_num
            cum_tgt_words += pred_tgt_word_num
            report_examples += batch_size
            cum_examples += batch_size
            cum_batches += batch_size

            if train_iter % args.log_every == 0:
                print('loss', loss_val)
                print('epoch %d, iter %d, avg. loss %.2f, avg. ppl %.2f ' \
                      'cum. examples %d, speed %.2f words/sec, time elapsed %.2f sec' % (epoch, train_iter,
                                                                                         report_loss / report_examples,
                                                                                         np.exp(report_loss / report_tgt_words),
                                                                                         cum_examples,
                                                                                         report_tgt_words / (time.time() - train_time),
                                                                                         time.time() - begin_time), file=sys.stderr)

                train_time = time.time()
                report_loss = report_tgt_words = report_examples = 0.
            # perform validation
            if train_iter % args.valid_niter  == 0:
            #if train_iter == 1:

                print('epoch %d, iter %d, cum. loss %.2f, cum. ppl %.2f cum. examples %d' % (epoch, train_iter,
                                                                                         cum_loss / cum_batches,
                                                                                         np.exp(cum_loss / cum_tgt_words),
                                                                                         cum_examples), file=sys.stderr)

                cum_loss = cum_batches = cum_tgt_words = 0.
                valid_num += 1

                print('begin validation ...', file=sys.stderr)
                model.eval()

                # compute dev. ppl and bleu
                print("Validation data:",len(dev_data))
                dev_loss = evaluate_loss(model, dev_data, cross_entropy_loss, use_teacher_forcing=True)
                dev_ppl = np.exp(dev_loss)

                if args.valid_metric in ['bleu', 'word_acc', 'sent_acc']:
                    dev_hyps, dev_tgt = decode(model, dev_data[:1500], verbose=False)
                    dev_hyps = [hyps[0] for hyps in dev_hyps]
                    if args.valid_metric == 'bleu':
                        # valid_metric = get_bleu([tgt for src, tgt in dev_data], dev_hyps)
                        valid_metric = compute_bleu([[tgt] for tgt in dev_tgt], dev_hyps)[0]
                        print('-'*10)
                        print('computed_bleu', valid_metric)
                        print('-'*10)
                    else:
                        valid_metric = get_acc([tgt for src, tgt in dev_data], dev_hyps, acc_type=args.valid_metric)
                    print('validation: iter %d, dev. ppl %f, dev. %s %f' % (train_iter, dev_ppl, args.valid_metric, valid_metric),
                          file=sys.stderr)
                else:
                    valid_metric = -dev_ppl
                    print('validation: iter %d, dev. ppl %f' % (train_iter, dev_ppl),
                          file=sys.stderr)

                model.train()
                for i in model.parameters():
                    i.requires_grad = False

                for i in model.actor.parameters():
                    i.requires_grad = True

                is_better = len(hist_valid_scores) == 0 or valid_metric > max(hist_valid_scores)
                is_better_than_last = len(hist_valid_scores) == 0 or valid_metric > hist_valid_scores[-1]
                hist_valid_scores.append(valid_metric)
                if valid_num > args.save_model_after:
                    model_file = './models/agent_gumb_train.bin'
                    print('save model to [%s]' % model_file, file=sys.stderr)
                    model.save(model_file)

                if (not is_better_than_last) and args.lr_decay:
                    lr = optimizer.param_groups[0]['lr'] * args.lr_decay
                    print('decay learning rate to %f' % lr, file=sys.stderr)
                    optimizer.param_groups[0]['lr'] = lr

                if is_better:
                    patience = 0
                    best_model_iter = train_iter

                    if valid_num > args.save_model_after:
                        print('save currently the best model ..', file=sys.stderr)
                        model_file_abs_path = os.path.abspath(model_file)
                        symlin_file_abs_path = os.path.abspath(args.save_to + '.bin')
                        os.system('ln -sf %s %s' % (model_file_abs_path, symlin_file_abs_path))
                else:
                    patience += 1
                    print('hit patience %d' % patience, file=sys.stderr)
                    if patience == args.patience:
                        print('early stop!', file=sys.stderr)
                        print('the best model is from iteration [%d]' % best_model_iter, file=sys.stderr)
                        exit(0)
            tqdm_bar.update(batch_size)
        tqdm_bar.close()


def read_raml_train_data(data_file, temp):
    train_data = dict()
    num_pattern = re.compile('^(\d+) samples$')
    with open(data_file) as f:
        while True:
            line = f.readline()
            if line is None or line == '':
                break

            assert line.startswith('***')

            src_sent = f.readline()[len('source: '):].strip()
            tgt_num = int(num_pattern.match(f.readline().strip()).group(1))
            tgt_samples = []
            tgt_scores = []
            for i in range(tgt_num):
                d = f.readline().strip().split(' ||| ')
                if len(d) < 2:
                    continue

                tgt_sent = d[0].strip()
                bleu_score = float(d[1])
                tgt_samples.append(tgt_sent)
                tgt_scores.append(bleu_score / temp)

            tgt_scores = np.exp(tgt_scores)
            tgt_scores = tgt_scores / np.sum(tgt_scores)

            tgt_entry = zip(tgt_samples, tgt_scores)
            train_data[src_sent] = tgt_entry

            line = f.readline()

    return train_data


def train_raml(args):
    tau = args.temp

    train_data_src = read_corpus(args.train_src, source='src')
    train_data_tgt = read_corpus(args.train_tgt, source='tgt')
    train_data = zip(train_data_src, train_data_tgt)

    dev_data_src = read_corpus(args.dev_src, source='src')
    dev_data_tgt = read_corpus(args.dev_tgt, source='tgt')
    dev_data = zip(dev_data_src, dev_data_tgt)

    vocab, model, optimizer, nll_loss, cross_entropy_loss = init_training(args)

    if args.raml_sample_mode == 'pre_sample':
        # dict of (src, [tgt: (sent, prob)])
        print('read in raml training data...', file=sys.stderr, end='')
        begin_time = time.time()
        raml_samples = read_raml_train_data(args.raml_sample_file, temp=tau)
        print('done[%d s].' % (time.time() - begin_time))
    elif args.raml_sample_mode.startswith('hamming_distance'):
        print('sample from hamming distance payoff distribution')
        payoff_prob, Z_qs = generate_hamming_distance_payoff_distribution(max(len(sent) for sent in train_data_tgt),
                                                                          vocab_size=len(vocab.tgt) - 3,
                                                                          tau=tau)

    train_iter = patience = cum_loss = report_loss = cum_tgt_words = report_tgt_words = 0
    report_weighted_loss = cum_weighted_loss = 0
    cum_examples = cum_batches = report_examples = epoch = valid_num = best_model_iter = 0
    hist_valid_scores = []
    train_time = begin_time = time.time()
    print('begin RAML training')

    # smoothing function for BLEU
    sm_func = None
    if args.smooth_bleu:
        sm_func = SmoothingFunction().method3

    while True:
        epoch += 1
        for src_sents, tgt_sents in data_iter(train_data, batch_size=args.batch_size):
            train_iter += 1

            raml_src_sents = []
            raml_tgt_sents = []
            raml_tgt_weights = []

            if args.raml_sample_mode == 'pre_sample':
                for src_sent in src_sents:
                    tgt_samples_all = raml_samples[' '.join(src_sent)]

                    if args.sample_size >= len(tgt_samples_all):
                        tgt_samples = tgt_samples_all
                    else:
                        tgt_samples_id = np.random.choice(range(1, len(tgt_samples_all)), size=args.sample_size - 1, replace=False)
                        tgt_samples = [tgt_samples_all[0]] + [tgt_samples_all[i] for i in tgt_samples_id] # make sure the ground truth y* is in the samples

                    raml_src_sents.extend([src_sent] * len(tgt_samples))
                    raml_tgt_sents.extend([['<s>'] + sent.split(' ') + ['</s>'] for sent, weight in tgt_samples])
                    raml_tgt_weights.extend([weight for sent, weight in tgt_samples])
            elif args.raml_sample_mode in ['hamming_distance', 'hamming_distance_impt_sample']:
                for src_sent, tgt_sent in zip(src_sents, tgt_sents):
                    tgt_samples = []  # make sure the ground truth y* is in the samples
                    tgt_sent_len = len(tgt_sent) - 3 # remove <s> and </s> and ending period .
                    tgt_ref_tokens = tgt_sent[1:-1]
                    bleu_scores = []
                    # print('y*: %s' % ' '.join(tgt_sent))
                    # sample an edit distances
                    e_samples = np.random.choice(range(tgt_sent_len + 1), p=payoff_prob[tgt_sent_len], size=args.sample_size, replace=True)

                    # make sure the ground truth y* is in the samples
                    if args.raml_bias_groundtruth and (not 0 in e_samples):
                        e_samples[0] = 0

                    for i, e in enumerate(e_samples):
                        if e > 0:
                            # sample a new tgt_sent $y$
                            old_word_pos = np.random.choice(range(1, tgt_sent_len + 1), size=e, replace=False)
                            new_words = [vocab.tgt.id2word[wid] for wid in np.random.randint(3, len(vocab.tgt), size=e)]
                            new_tgt_sent = list(tgt_sent)
                            for pos, word in zip(old_word_pos, new_words):
                                new_tgt_sent[pos] = word
                        else:
                            new_tgt_sent = list(tgt_sent)

                        # if enable importance sampling, compute bleu score
                        if args.raml_sample_mode == 'hamming_distance_impt_sample':
                            if e > 0:
                                # remove <s> and </s>
                                bleu_score = sentence_bleu([tgt_ref_tokens], new_tgt_sent[1:-1], smoothing_function=sm_func)
                                bleu_scores.append(bleu_score)
                            else:
                                bleu_scores.append(1.)

                        # print('y: %s' % ' '.join(new_tgt_sent))
                        tgt_samples.append(new_tgt_sent)

                    # if enable importance sampling, compute importance weight
                    if args.raml_sample_mode == 'hamming_distance_impt_sample':
                        tgt_sample_weights = [math.exp(bleu_score / tau) / math.exp(-e / tau) for e, bleu_score in zip(e_samples, bleu_scores)]
                        normalizer = sum(tgt_sample_weights)
                        tgt_sample_weights = [w / normalizer for w in tgt_sample_weights]
                    else:
                        tgt_sample_weights = [1.] * args.sample_size

                    if args.debug:
                        print('*' * 30)
                        print('Target: %s' % ' '.join(tgt_sent))
                        for tgt_sample, e, bleu_score, weight in zip(tgt_samples, e_samples, bleu_scores,
                                                                     tgt_sample_weights):
                            print('Sample: %s ||| e: %d ||| bleu: %f ||| weight: %f' % (
                            ' '.join(tgt_sample), e, bleu_score, weight))
                        print()

                    raml_src_sents.extend([src_sent] * len(tgt_samples))
                    raml_tgt_sents.extend(tgt_samples)
                    raml_tgt_weights.extend(tgt_sample_weights)

            src_sents_var = to_input_variable(raml_src_sents, vocab.src, cuda=args.cuda)
            tgt_sents_var = to_input_variable(raml_tgt_sents, vocab.tgt, cuda=args.cuda)
            weights_var = torch.FloatTensor(raml_tgt_weights)
            if args.cuda:
                weights_var = weights_var.cuda()

            batch_size = len(raml_src_sents)  # batch_size = args.batch_size * args.sample_size
            src_sents_len = [len(s) for s in raml_src_sents]
            pred_tgt_word_num = sum(len(s[1:]) for s in raml_tgt_sents)  # omitting leading `<s>`
            optimizer.zero_grad()

            # (tgt_sent_len, batch_size, tgt_vocab_size)
            scores = model(src_sents_var, src_sents_len, tgt_sents_var[:-1])
            # (tgt_sent_len * batch_size, tgt_vocab_size)
            log_scores = F.log_softmax(scores.view(-1, scores.size(2)))
            # remove leading <s> in tgt sent, which is not used as the target
            flattened_tgt_sents = tgt_sents_var[1:].view(-1)

            # batch_size * tgt_sent_len
            tgt_log_scores = torch.gather(log_scores, 1, flattened_tgt_sents.unsqueeze(1)).squeeze(1)
            unweighted_loss = -tgt_log_scores * (1. - torch.eq(flattened_tgt_sents, 0).float())
            weighted_loss = unweighted_loss * weights_var.repeat(scores.size(0))
            weighted_loss = weighted_loss.sum()
            weighted_loss_val = weighted_loss.data[0]
            nll_loss_val = unweighted_loss.sum().data[0]
            # weighted_log_scores = log_scores * weights.view(-1, scores.size(2))
            # weighted_loss = nll_loss(weighted_log_scores, flattened_tgt_sents)

            loss = weighted_loss / batch_size
            # nll_loss_val = nll_loss(log_scores, flattened_tgt_sents).data[0]

            loss.backward()
            # clip gradient
            grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), args.clip_grad)
            optimizer.step()

            report_weighted_loss += weighted_loss_val
            cum_weighted_loss += weighted_loss_val
            report_loss += nll_loss_val
            cum_loss += nll_loss_val
            report_tgt_words += pred_tgt_word_num
            cum_tgt_words += pred_tgt_word_num
            report_examples += batch_size
            cum_examples += batch_size
            cum_batches += batch_size

            if train_iter % args.log_every == 0:
                print('epoch %d, iter %d, avg. loss %.2f, '
                      'avg. ppl %.2f cum. examples %d, '
                      'speed %.2f words/sec, time elapsed %.2f sec' % (epoch, train_iter,
                                                                       report_weighted_loss / report_examples,
                                                                       np.exp(report_loss / report_tgt_words),
                                                                       cum_examples,
                                                                       report_tgt_words / (time.time() - train_time),
                                                                       time.time() - begin_time),
                      file=sys.stderr)

                train_time = time.time()
                report_loss = report_weighted_loss = report_tgt_words = report_examples = 0.

            # perform validation
            if train_iter % args.valid_niter == 0:
                print('epoch %d, iter %d, cum. loss %.2f, '
                      'cum. ppl %.2f cum. examples %d' % (epoch, train_iter,
                                                          cum_weighted_loss / cum_batches,
                                                          np.exp(cum_loss / cum_tgt_words),
                                                          cum_examples),
                      file=sys.stderr)

                cum_loss = cum_weighted_loss = cum_batches = cum_tgt_words = 0.
                valid_num += 1

                print('begin validation ...', file=sys.stderr)
                model.eval()

                # compute dev. ppl and bleu

                dev_loss = evaluate_loss(model, dev_data, cross_entropy_loss)
                dev_ppl = np.exp(dev_loss)

                if args.valid_metric in ['bleu', 'word_acc', 'sent_acc']:
                    dev_hyps = decode(model, dev_data)
                    dev_hyps = [hyps[0] for hyps in dev_hyps]
                    if args.valid_metric == 'bleu':
                        valid_metric = get_bleu([tgt for src, tgt in dev_data], dev_hyps)
                    else:
                        valid_metric = get_acc([tgt for src, tgt in dev_data], dev_hyps, acc_type=args.valid_metric)
                    print('validation: iter %d, dev. ppl %f, dev. %s %f' % (
                    train_iter, dev_ppl, args.valid_metric, valid_metric),
                          file=sys.stderr)
                else:
                    valid_metric = -dev_ppl
                    print('validation: iter %d, dev. ppl %f' % (train_iter, dev_ppl),
                          file=sys.stderr)

                model.train()

                is_better = len(hist_valid_scores) == 0 or valid_metric > max(hist_valid_scores)
                is_better_than_last = len(hist_valid_scores) == 0 or valid_metric > hist_valid_scores[-1]
                hist_valid_scores.append(valid_metric)

                if valid_num > args.save_model_after:
                    model_file = args.save_to + '.iter%d.bin' % train_iter
                    print('save model to [%s]' % model_file, file=sys.stderr)
                    model.save(model_file)

                if (not is_better_than_last) and args.lr_decay:
                    lr = optimizer.param_groups[0]['lr'] * args.lr_decay
                    print('decay learning rate to %f' % lr, file=sys.stderr)
                    optimizer.param_groups[0]['lr'] = lr

                if is_better:
                    patience = 0
                    best_model_iter = train_iter

                    if valid_num > args.save_model_after:
                        print('save currently the best model ..', file=sys.stderr)
                        model_file_abs_path = os.path.abspath(model_file)
                        symlin_file_abs_path = os.path.abspath(args.save_to + '.bin')
                        os.system('ln -sf %s %s' % (model_file_abs_path, symlin_file_abs_path))
                else:
                    patience += 1
                    print('hit patience %d' % patience, file=sys.stderr)
                    if patience == args.patience:
                        print('early stop!', file=sys.stderr)
                        print('the best model is from iteration [%d]' % best_model_iter, file=sys.stderr)
                        exit(0)


def get_bleu(references, hypotheses):
    # compute BLEU
    bleu_score = corpus_bleu([[ref[1:-1]] for ref in references],
                             [hyp[1:-1] for hyp in hypotheses])

    return bleu_score


def get_acc(references, hypotheses, acc_type='word'):
    assert acc_type == 'word_acc' or acc_type == 'sent_acc'
    cum_acc = 0.

    for ref, hyp in zip(references, hypotheses):
        ref = ref[1:-1]
        hyp = hyp[1:-1]
        if acc_type == 'word_acc':
            acc = len([1 for ref_w, hyp_w in zip(ref, hyp) if ref_w == hyp_w]) / float(len(hyp) + 1e-6)
        else:
            acc = 1. if all(ref_w == hyp_w for ref_w, hyp_w in zip(ref, hyp)) else 0.
        cum_acc += acc

    acc = cum_acc / len(hypotheses)
    return acc


def decode(model, data, batch_size=1, verbose=True, beam_size=None):
    """
    decode the dataset and compute sentence level acc. and BLEU.
    """
    hypotheses = []
    targets = []
    begin_time = time.time()
    data = data
    if beam_size is None:
        beam_size = args.beam_size
    print("decode with beam size:", beam_size)
    if batch_size != 1:
        print("batch_size != 1")
        di = data_iter(data, batch_size=batch_size, shuffle=False)
        tqdm_bar = tqdm(total=len(data))
        for src_sent, tgt_sent in di:
            size = len(src_sent)
            batch_hyps = model.translate_batch(src_sent, beam_size=beam_size)
            #total_scores = model.relax_decode(src_sent, beam_size=args.beam_size)
            for i in range(len(batch_hyps)):
                hypotheses.append(batch_hyps[i])
                targets.append(tgt_sent[i])
                if verbose:
                    print('*' * 50)
                    print('Source: ', ' '.join(src_sent[i]))
                    print('Target: ', ' '.join(tgt_sent[i][1:]))#without bos
                    print('Top Hypothesis: ', ' '.join(batch_hyps[i][0][1:]))
            tqdm_bar.update(size)
        tqdm_bar.close()
    else:
        tqdm_bar = tqdm(total=len(data))
        for src_sent, tgt_sent in data:
            hyps = model.translate(src_sent)
            hypotheses.append(hyps)
            targets.append(tgt_sent)
            if verbose:
                print('*' * 50)
                print('Source: ', ' '.join(src_sent))
                print('Target: ', ' '.join(tgt_sent[1:]))  # without bos
                print('Top Hypothesis: ', ' '.join(hyps[0][1:]))
            tqdm_bar.update(1)
        tqdm_bar.close()
    elapsed = time.time() - begin_time

    print('decoded %d examples, took %d s' % (len(data), elapsed), file=sys.stderr)

    return hypotheses, targets


def compute_lm_prob(args):
    """
    given source-target sentence pairs, compute ppl and log-likelihood
    """
    test_data_src = read_corpus(args.test_src, source='src')
    test_data_tgt = read_corpus(args.test_tgt, source='tgt')
    test_data = zip(test_data_src, test_data_tgt)

    if args.load_model:
        print('load model from [%s]' % args.load_model, file=sys.stderr)
        params = torch.load(args.load_model, map_location=lambda storage, loc: storage)
        vocab = params['vocab']
        saved_args = params['args']
        state_dict = params['state_dict']

        model = NMT(saved_args, vocab)
        model.load_state_dict(state_dict)
    else:
        vocab = torch.load(args.vocab)
        model = NMT(args, vocab)

    model.eval()

    if args.cuda:
        model = model.cuda()

    f = open(args.save_to_file, 'w')
    for src_sent, tgt_sent in test_data:
        src_sents = [src_sent]
        tgt_sents = [tgt_sent]

        batch_size = len(src_sents)
        src_sents_len = [len(s) for s in src_sents]
        pred_tgt_word_nums = [len(s[1:]) for s in tgt_sents]  # omitting leading `<s>`

        # (sent_len, batch_size)
        src_sents_var = to_input_variable(src_sents, model.vocab.src, cuda=args.cuda, is_test=True)
        tgt_sents_var = to_input_variable(tgt_sents, model.vocab.tgt, cuda=args.cuda, is_test=True)

        # (tgt_sent_len, batch_size, tgt_vocab_size)
        scores = model(src_sents_var, src_sents_len, tgt_sents_var[:-1])
        # (tgt_sent_len * batch_size, tgt_vocab_size)
        log_scores = F.log_softmax(scores.view(-1, scores.size(2)))
        # remove leading <s> in tgt sent, which is not used as the target
        # (batch_size * tgt_sent_len)
        flattened_tgt_sents = tgt_sents_var[1:].view(-1)
        # (batch_size * tgt_sent_len)
        tgt_log_scores = torch.gather(log_scores, 1, flattened_tgt_sents.unsqueeze(1)).squeeze(1)
        # 0-index is the <pad> symbol
        tgt_log_scores = tgt_log_scores * (1. - torch.eq(flattened_tgt_sents, 0).float())
        # (tgt_sent_len, batch_size)
        tgt_log_scores = tgt_log_scores.view(-1, batch_size) # .permute(1, 0)
        # (batch_size)
        tgt_sent_scores = tgt_log_scores.sum(dim=0).squeeze()
        tgt_sent_word_scores = [tgt_sent_scores[i].data[0] / pred_tgt_word_nums[i] for i in range(batch_size)]

        for src_sent, tgt_sent, score in zip(src_sents, tgt_sents, tgt_sent_word_scores):
            f.write('%s ||| %s ||| %f\n' % (' '.join(src_sent), ' '.join(tgt_sent), score))

    f.close()


def test(args):
    test_data_src = read_corpus(args.test_src, source='src')
    test_data_tgt = read_corpus(args.test_tgt, source='tgt')
    test_data = list(zip(test_data_src, test_data_tgt))

    if args.load_model:
        print('load model from [%s]' % args.load_model, file=sys.stderr)
        params = torch.load(args.load_model, map_location=lambda storage, loc: storage)
        vocab = params['vocab']
        saved_args = params['args']
        state_dict = params['state_dict']

        model = NMT(saved_args, vocab).to(device)
        model.load_state_dict(state_dict)
    else:
        vocab = torch.load(args.vocab)
        model = NMT(args, vocab).to(device)

    model.eval()
    model.to(device)

    hypotheses, targets = decode(model, test_data, batch_size=1, verbose=False)
    top_hypotheses = [hyps[0] for hyps in hypotheses]

    # print(top_hypotheses)
    # print("???" * 25)
    # print(targets)
    bleu_score = compute_bleu([[tgt] for tgt in targets], top_hypotheses)
    word_acc = get_acc([tgt for src, tgt in test_data], top_hypotheses, 'word_acc')
    sent_acc = get_acc([tgt for src, tgt in test_data], top_hypotheses, 'sent_acc')
    print('-'*10)
    print(bleu_score, word_acc, sent_acc)
    print('Corpus Level BLEU: %f, word level acc: %f, sentence level acc: %f' % (bleu_score[0], word_acc, sent_acc),
          file=sys.stderr)

    if args.save_to_file:
        print('save decoding results to %s' % args.save_to_file, file=sys.stderr)
        with open(args.save_to_file, 'w') as f:
            for hyps in hypotheses:
                f.write(' '.join(hyps[0][1:-1]) + '\n')

        with open(args.save_to_file + ".targets",'w') as f:
            for tgt in targets:
                f.write(' '.join(tgt[1:-1]) + '\n')

        if args.save_nbest:
            nbest_file = args.save_to_file + '.nbest'
            print('save nbest decoding results to %s' % nbest_file, file=sys.stderr)
            with open(nbest_file, 'w') as f:
                for src_sent, tgt_sent, hyps in zip(test_data_src, test_data_tgt, hypotheses):
                    print('Source: %s' % ' '.join(src_sent), file=f)
                    print('Target: %s' % ' '.join(tgt_sent), file=f)
                    print('Hypotheses:', file=f)
                    for i, hyp in enumerate(hyps, 1):
                        print('[%d] %s' % (i, ' '.join(hyp)), file=f)
                    print('*' * 30, file=f)


def interactive(args):
    assert args.load_model, 'You have to specify a pre-trained model'
    print('load model from [%s]' % args.load_model, file=sys.stderr)
    params = torch.load(args.load_model, map_location=lambda storage, loc: storage)
    vocab = params['vocab']
    saved_args = params['args']
    state_dict = params['state_dict']

    model = NMT(saved_args, vocab)
    model.load_state_dict(state_dict)

    model.eval()

    if args.cuda:
        model = model.cuda()

    while True:
        src_sent = input('Source Sentence:')
        src_sent = src_sent.strip().split(' ')
        hyps = model.translate(src_sent)
        for i, hyp in enumerate(hyps, 1):
            print('Hypothesis #%d: %s' % (i, ' '.join(hyp)))


def sample(args):
    train_data_src = read_corpus(args.train_src, source='src')
    train_data_tgt = read_corpus(args.train_tgt, source='tgt')
    train_data = zip(train_data_src, train_data_tgt)

    if args.load_model:
        print('load model from [%s]' % args.load_model, file=sys.stderr)
        params = torch.load(args.load_model, map_location=lambda storage, loc: storage)
        vocab = params['vocab']
        opt = params['args']
        state_dict = params['state_dict']

        model = NMT(opt, vocab)
        model.load_state_dict(state_dict)
    else:
        vocab = torch.load(args.vocab)
        model = NMT(args, vocab)

    model.eval()

    if args.cuda:
        model = model.cuda()

    print('begin sampling')

    check_every = 10
    train_iter = cum_samples = 0
    train_time = time.time()
    for src_sents, tgt_sents in data_iter(train_data, batch_size=args.batch_size):
        train_iter += 1
        samples = model.sample(src_sents, sample_size=args.sample_size, to_word=True)
        cum_samples += sum(len(sample) for sample in samples)

        if train_iter % check_every == 0:
            elapsed = time.time() - train_time
            print('sampling speed: %d/s' % (cum_samples / elapsed), file=sys.stderr)
            cum_samples = 0
            train_time = time.time()

        for i, tgt_sent in enumerate(tgt_sents):
            print('*' * 80)
            print('target:' + ' '.join(tgt_sent))
            tgt_samples = samples[i]
            print('samples:')
            for sid, sample in enumerate(tgt_samples, 1):
                print('[%d] %s' % (sid, ' '.join(sample[1:-1])))
            print('*' * 80)


if __name__ == '__main__':
    args = init_config()
    #sys.settrace(gpu_profile)
    print(args, file=sys.stderr)
    global device
    device = torch.device("cuda" if args.cuda else "cpu")
    if args.mode == 'train':
        train(args)
    elif args.mode == 'custom':
        custom_train(args)
    elif args.mode == 'custom2':
        custom_train2(args)
    elif args.mode == 'custom3':
        custom_train3(args)
    elif args.mode == 'train_sampling':
        train_with_sampling(args)
    elif args.mode == 'train_actor':
        train_actor(args)
    elif args.mode == 'relax_train':
        relax_train(args)
    elif args.mode == 'raml_train':
        train_raml(args)
    elif args.mode == 'sample':
        sample(args)
    elif args.mode == 'test':
        test(args)
    elif args.mode == 'prob':
        compute_lm_prob(args)
    elif args.mode == 'interactive':
        interactive(args)
    else:
        raise RuntimeError('unknown mode')
