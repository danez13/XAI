"""Script to serialize the saliency with gradient approaches and occlusion."""
# Import necessary libraries
import argparse
import json
import os
import random
from argparse import Namespace
from collections import defaultdict
from functools import partial

import numpy as np
import torch
from captum.attr import DeepLift, GuidedBackprop, InputXGradient, Occlusion, \
    Saliency, configure_interpretable_embedding_layer, \
    remove_interpretable_embedding_layer
from torch.utils.data import DataLoader
from tqdm import tqdm
from transformers import BertTokenizer

from models.data_loader import collate_nli, NLIDataset
from models.model_builder import CNN_MODEL
import time

# Function to summarize attributions generated by saliency methods
def summarize_attributions(attributions, type='mean', model=None, tokens=None):
    # Handle different types of summarization for saliency attributions
    if type == 'none':
        return attributions
    elif type == 'dot':
        embeddings = get_model_embedding_emb(model)(tokens)
        attributions = torch.einsum('bwd, bwd->bw', attributions, embeddings)
    elif type == 'mean':
        attributions = attributions.mean(dim=-1).squeeze(0)  # Averaging along the last dimension
        attributions = attributions / torch.norm(attributions)  # Normalizing the attributions
    elif type == 'l2':
        attributions = attributions.norm(p=1, dim=-1).squeeze(0)  # L2 norm computation for the attributions
    return attributions

# Wrapper class for BERT model to extract embeddings
class BertModelWrapper(torch.nn.Module):
    def __init__(self, model):
        super(BertModelWrapper, self).__init__()
        self.model = model

    def forward(self, input, attention_mask, labels):
        return self.model(input, attention_mask=attention_mask)[0]

# Function to get the embedding layer of the model
def get_model_embedding_emb(model):
    return model.embedding.embedding

# Function to generate saliency maps for different methods
def generate_saliency(model_path, saliency_path, saliency, aggregation):
    # Load the model checkpoint
    checkpoint = torch.load(model_path, map_location=lambda storage, loc: storage)
    model_args = checkpoint['args']
    
    # Initialize model using the provided arguments
    model = CNN_MODEL(tokenizer, model_args, n_labels=checkpoint['args']['labels']).to(device)
    model.load_state_dict(checkpoint['model'])
    model.train()  # Set the model to training mode

    pad_to_max = False  # Padding options (not used here)
    
    # Select the appropriate attribution method
    if saliency == 'deeplift':
        ablator = DeepLift(model)
    elif saliency == 'guided':
        ablator = GuidedBackprop(model)
    elif saliency == 'sal':
        ablator = Saliency(model)
    elif saliency == 'inputx':
        ablator = InputXGradient(model)
    elif saliency == 'occlusion':
        ablator = Occlusion(model)

    # Prepare data loader for the test dataset
    collate_fn = partial(collate_nli, tokenizer=tokenizer, device=device,
                         return_attention_masks=False, pad_to_max_length=pad_to_max)
    test = NLIDataset(args["dataset_dir"], type=args["split"], salient_features=True)
    batch_size = args["batch_size"] if args["batch_size"] is not None else model_args['batch_size']
    test_dl = DataLoader(batch_size=batch_size, dataset=test, shuffle=False, collate_fn=collate_fn)

    # Generate predictions and store them if not already saved
    predictions_path = model_path + '.predictions'
    if not os.path.exists(predictions_path):
        predictions = defaultdict(lambda: [])
        for batch in tqdm(test_dl, desc='Running test prediction... '):
            logits = model(batch[0])  # Get logits from the model
            logits = logits.detach().cpu().numpy().tolist()  # Convert logits to CPU numpy array
            predicted = np.argmax(np.array(logits), axis=-1)  # Get predicted class labels
            predictions['class'] += predicted.tolist()
            predictions['logits'] += logits

        # Save predictions as JSON
        with open(predictions_path, 'w') as out:
            json.dump(predictions, out)

    # Set up saliency computation
    if saliency != 'occlusion':
        embedding_layer_name = 'embedding'
        interpretable_embedding = configure_interpretable_embedding_layer(model, embedding_layer_name)

    # Store attribution results
    class_attr_list = defaultdict(lambda: [])
    token_ids = []
    saliency_flops = []  # List to track processing time per batch

    # Process test batches and compute saliency attributions
    for batch in tqdm(test_dl, desc='Running Saliency Generation...'):
        additional = None
        token_ids += batch[0].detach().cpu().numpy().tolist()  # Store token ids
        if saliency != 'occlusion':
            input_embeddings = interpretable_embedding.indices_to_embeddings(batch[0])

        start = time.time()  # Start timer for FLOP computation

        # Compute attribution for each class
        for cls_ in range(checkpoint['args']['labels']):
            if saliency == 'occlusion':
                attributions = ablator.attribute(batch[0], sliding_window_shapes=(args["sw"],), target=cls_, additional_forward_args=additional)
            else:
                attributions = ablator.attribute(input_embeddings, target=cls_, additional_forward_args=additional)

            attributions = summarize_attributions(attributions, type=aggregation, model=model, tokens=batch[0]).detach().cpu().numpy().tolist()
            class_attr_list[cls_] += [[_li for _li in _l] for _l in attributions]  # Store class-wise attributions

        end = time.time()  # End timer
        saliency_flops.append((end - start) / batch[0].shape[0])  # Compute FLOPS for this batch

    if saliency != 'occlusion':
        remove_interpretable_embedding_layer(model, interpretable_embedding)

    # Serialize the saliency attributions to disk
    print('Serializing...', flush=True)
    with open(saliency_path, 'w') as out:
        for instance_i, _ in enumerate(test):
            saliencies = []
            for token_i, token_id in enumerate(token_ids[instance_i]):
                token_sal = {'token': tokenizer.ids_to_tokens[token_id]}  # Map token ids to actual tokens
                for cls_ in range(checkpoint['args']['labels']):
                    token_sal[int(cls_)] = class_attr_list[cls_][instance_i][token_i]
                saliencies.append(token_sal)

            out.write(json.dumps({'tokens': saliencies}) + '\n')  # Write token-wise saliency to file
            out.flush()

    return saliency_flops  # Return the computed FLOPS

# Set arguments for the script
args = {
    "dataset": "snli",  # Dataset name
    "dataset_dir": "data/e-SNLI/dataset/",  # Directory containing the dataset
    "split": "test",  # Split for evaluation (test set)
    "model": "cnn",  # Type of model
    "models_dir": ["data/models/snli/cnn/cnn", "data/models/snli/random_cnn/cnn"],  # Directories for the models
    "gpu": False,  # Whether to use GPU
    "seed": 73,  # Random seed for reproducibility
    "output_dir": ["data/saliency/snli/cnn/", "data/saliency/snli/random_cnn/"],  # Output directories for saliency
    "sw": 1,  # Sliding window size for occlusion
    "saliency": ["guided", "sal", "inputx", "occlusion"],  # List of saliency methods to use
    "batch_size": None  # Batch size for testing
}

# Set seeds for reproducibility
random.seed(args["seed"])
torch.manual_seed(args["seed"])
torch.cuda.manual_seed_all(args["seed"])
torch.backends.cudnn.deterministic = True
np.random.seed(args["seed"])

# Set device for model execution
device = torch.device("cuda") if args["gpu"] else torch.device("cpu")

# Initialize the BERT tokenizer
tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')

# Loop over different saliency methods
for saliency in args["saliency"]:
    print('Running Saliency ', saliency, flush=True)

    # Define the aggregation methods based on saliency type
    if saliency in ['guided', 'sal', 'inputx', 'deeplift']:
        aggregations = ['mean', 'l2']
    else:  # occlusion
        aggregations = ['none']

    # Loop over different aggregation methods
    for aggregation in aggregations:
        flops = []  # List to track average FLOPS per model
        print('Running aggregation ', aggregation, flush=True)

        # Loop over different model directories and corresponding output directories
        for models_dir, output_dir in zip(args["models_dir"], args["output_dir"]):
            base_model_name = models_dir.split('/')[-1]  # Extract the base model name
            # Loop over models (from 1 to 5)
            for model in range(1, 6):
                curr_flops = generate_saliency(
                    os.path.join(models_dir + f'_{model}'),
                    os.path.join(output_dir, f'{base_model_name}_{model}_{saliency}_{aggregation}'),
                    saliency,
                    aggregation)

                flops.append(np.average(curr_flops))  # Append FLOPS

            # Print the average and standard deviation of FLOPS
            print('FLOPS', np.average(flops), np.std(flops), flush=True)
            print()
            print()
