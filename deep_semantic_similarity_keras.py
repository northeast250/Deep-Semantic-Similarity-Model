# Michael A. Alcorn (malcorn@redhat.com)
# An implementation of the Deep Semantic Similarity Model (DSSM) found in [1].
# [1] Shen, Y., He, X., Gao, J., Deng, L., and Mesnil, G. 2014. A latent semantic model
#         with convolutional-pooling structure for information retrieval. In CIKM, pp. 101-110.
#         http://research.microsoft.com/pubs/226585/cikm2014_cdssm_final.pdf
# [2] http://research.microsoft.com/en-us/projects/dssm/
# [3] http://research.microsoft.com/pubs/238873/wsdm2015.v3.pdf

import numpy as np
import random

from keras import backend
from keras.layers import Input, merge
from keras.layers.core import Dense, Lambda, Reshape
from keras.layers.convolutional import Convolution1D
from keras.models import Model


def R(vects):
    """
    Calculates the cosine similarity of two vectors.
    :param vects: a list of two vectors.
    :return: the cosine similarity of two vectors.
    """
    (x, y) = vects
    return backend.dot(x, backend.transpose(y)) / (x.norm(2) * y.norm(2)) # See equation (4)


LETTER_GRAM_SIZE = 3 # See section 3.2.
N_GRAM_SIZE = 3 # See section 3.2.
TOTAL_LETTER_GRAMS = int(3 * 1e4) # Determined from data. See section 3.2.
WORD_DEPTH = N_GRAM_SIZE * TOTAL_LETTER_GRAMS # See equation (1).
K = 300 # Dimensionality of the max pooling layer. See section 3.4.
L = 128 # Dimensionality of latent semantic space. See section 3.5.
J = 4 # Number of random unclicked documents serving as negative examples for a query. See section 4.
FILTER_LENGTH = 1 # We only consider one time step for convolutions.

# Input tensors holding the query, positive (clicked) document, and negative (unclicked) documents.
# The first dimension is None because the queries and documents can vary in length.
query = Input(shape = (None, WORD_DEPTH))
pos_doc = Input(shape = (None, WORD_DEPTH))
neg_docs = [Input(shape = (None, WORD_DEPTH)) for j in range(J)]

# Query model. The paper uses separate neural nets for queries and documents (see section 5.2).

# In this step, we transform each word vector with WORD_DEPTH dimensions into its
# convolved representation with K dimensions. K is the number of kernels/filters
# being used in the operation. Essentially, the operation is taking the dot product
# of a single weight matrix (W_c) with each of the word vectors (l_t) from the
# query matrix (l_Q), adding a bias vector (b_c), and then applying the tanh function.
# That is, h_Q = tanh(W_c • l_Q + b_c). With that being said, that's not actually
# how the operation is being calculated here. To tie the weights of the weight
# matrix (W_c) together, we have to use a one-dimensional convolutional layer. 
# Further, we have to transpose our query matrix (l_Q) so that time is the first
# dimension rather than the second (as described in the paper). That is, l_Q[0, :]
# represents our first word vector rather than l_Q[:, 0]. We can think of the weight
# matrix (W_c) as being similarly transposed such that each kernel is a column
# of W_c. Therefore, h_Q = tanh(l_Q • W_c + b_c) with l_Q, W_c, and b_c being
# the transposes of the matrices described in the paper.
query_conv = Convolution1D(K, FILTER_LENGTH, border_mode = "same", input_shape = (None, WORD_DEPTH), activation = "tanh")(query) # See equation (2).

# Next, we apply a max-pooling layer to the convolved query matrix. Keras provides
# its own max-pooling layers, but they cannot handle variable length input (as
# far as I can tell). As a result, I define my own max-pooling layer here. In the
# paper, the operation selects the maximum value for each row of h_Q, but, because
# we're using the transpose, we're selecting the maximum value for each column.
query_max = Lambda(lambda x: x.max(axis = 1), output_shape = (K,))(query_conv) # See section 3.4.

# In this step, we generate the semantic vector represenation of the query. This
# is a standard neural network dense layer, i.e., y = tanh(W_s • v + b_s).
query_sem = Dense(L, activation = "tanh", input_dim = K)(query_max) # See section 3.5.

# The document equivalent of the above query model.
doc_conv = Convolution1D(K, FILTER_LENGTH, border_mode = "same", input_shape = (None, WORD_DEPTH), activation = "tanh")
doc_max = Lambda(lambda x: x.max(axis = 1), output_shape = (K,))
doc_sem = Dense(L, activation = "tanh", input_dim = K)

pos_doc_conv = doc_conv(pos_doc)
neg_doc_convs = [doc_conv(neg_doc) for neg_doc in neg_docs]

pos_doc_max = doc_max(pos_doc_conv)
neg_doc_maxes = [doc_max(neg_doc_conv) for neg_doc_conv in neg_doc_convs]

pos_doc_sem = doc_sem(pos_doc_max)
neg_doc_sems = [doc_sem(neg_doc_max) for neg_doc_max in neg_doc_maxes]

# This layer calculates the cosine similarity between the semantic representations of
# a query and a document.
R_layer = Lambda(R, output_shape = (1,)) # See equation (4).

R_Q_D_p = R_layer([query_sem, pos_doc_sem]) # See equation (4).
R_Q_D_ns = [R_layer([query_sem, neg_doc_sem]) for neg_doc_sem in neg_doc_sems] # See equation (4).

concat_Rs = merge([R_Q_D_p] + R_Q_D_ns, mode = "concat")
concat_Rs = Reshape((J + 1, 1))(concat_Rs)

# In this step, we multiply each R(Q, D) value by gamma. In the paper, gamma is
# described as a smoothing factor for the softmax function, and it's set empirically
# on a held-out data set. We're going to learn gamma's value by pretending it's
# a single, 1 x 1 kernel.
with_gamma = Convolution1D(1, 1, border_mode = "same", input_shape = (J + 1, 1), activation = "linear")(concat_Rs) # See equation (5).

# Next, we exponentiate each of the gamma x R(Q, D) values.
exponentiated = Lambda(lambda x: backend.exp(x), output_shape = (J + 1,))(with_gamma) # See equation (5).
exponentiated = Reshape((J + 1,))(exponentiated)

# Finally, we use the softmax function to calculate the P(D+|Q).
prob = Lambda(lambda x: x[0][0] / backend.sum(x[0]), output_shape = (1,))(exponentiated) # See equation (5).

# We now have everything we need to define our model.
model = Model(input = [query, pos_doc] + neg_docs, output = prob)
model.compile(optimizer = "adadelta", loss = "binary_crossentropy")

# Build a random data set.
sample_size = 10
l_Qs = []
pos_l_Ds = []

for i in range(sample_size):
    query_len = random.randint(1, 10)
    l_Q = np.random.rand(1, query_len, WORD_DEPTH)
    l_Qs.append(l_Q)
    
    doc_len = random.randint(50, 500)
    l_D = np.random.rand(1, doc_len, WORD_DEPTH)
    pos_l_Ds.append(l_D)

neg_l_Ds = []
for i in range(sample_size):
    possibilities = list(range(sample_size))
    possibilities.remove(i)
    negatives = random.sample(possibilities, J)
    neg_l_Ds.append([pos_l_Ds[negative] for negative in negatives])

# Because we're using the "binary_crossentropy" loss function, we can pretend that
# we're solving a binary classification problem and that every sample is a member
# of the "1" class.
y = np.ones(1)

for i in range(sample_size):
    history = model.fit([l_Qs[i], pos_l_Ds[i]] + neg_l_Ds[i], y, nb_epoch = 1, verbose = 0)

# Here, I walk through an example of how to define a function for calculating output
# from the computational graph. Let's define a function that calculates R(Q, D+)
# for a given query and clicked document. The function depends on two inputs, query
# and pos_doc. That is, if you start at the point in the graph where R(Q, D+) is
# calculated and then backtrack as far as possible, you'll end up at two different
# starting points, query and pos_doc. As a result, we supply those inputs in a list
# to the function. This particular function only calculates a single output, but
# multiple outputs are possible (see the next example).
get_R_Q_D_p = backend.function([query, pos_doc], R_Q_D_p)
get_R_Q_D_p([l_Qs[0], pos_l_Ds[0]])

# A slightly more complex function. Notice that both neg_docs and our output are
# lists.
get_R_Q_D_ns = backend.function([query] + neg_docs, R_Q_D_ns)
get_R_Q_D_ns([l_Qs[0]] + neg_l_Ds[0])
