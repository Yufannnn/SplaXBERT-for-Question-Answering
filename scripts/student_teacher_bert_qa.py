# qa_distillation.py

import json
import torch
from torch.utils.data import Dataset, DataLoader
from transformers import BertTokenizerFast, BertForQuestionAnswering, AdamW
from tqdm import tqdm
import collections
import os
import torch.nn.functional as F

# Check if GPU is available
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print(f'Using device: {device}')

# Load the data
def read_squad(path):
    with open(path, 'r') as f:
        squad = json.load(f)
    data = []
    for article in squad['data']:
        for paragraph in article['paragraphs']:
            context = paragraph['context']
            for qa in paragraph['qas']:
                question = qa['question']
                qid = qa['id']
                answers = qa['answers']
                if len(answers) == 0:
                    continue  # Skip unanswerable questions
                answer_text = answers[0]['text']
                answer_start = answers[0]['answer_start']
                data.append({
                    'context': context,
                    'question': question,
                    'qid': qid,
                    'answer_text': answer_text,
                    'answer_start': answer_start
                })
    return data

# Load training and validation data
train_data = read_squad('../data/train-v1.1.json')
dev_data = read_squad('../data/dev-v1.1.json')

# Create a custom dataset
class SquadDataset(Dataset):
    def __init__(self, data, tokenizer, max_length=384, doc_stride=128):
        self.examples = []
        for item in tqdm(data, desc="Processing Data"):
            inputs = tokenizer(
                item['question'],
                item['context'],
                max_length=max_length,
                truncation='only_second',
                stride=doc_stride,
                return_overflowing_tokens=True,
                return_offsets_mapping=True,
                padding='max_length'
            )
            offset_mapping = inputs.pop('offset_mapping')
            sample_mapping = inputs.pop('overflow_to_sample_mapping')
            answers = item['answer_text']
            start_char = item['answer_start']
            end_char = start_char + len(answers)
            for i in range(len(inputs['input_ids'])):
                input_ids = inputs['input_ids'][i]
                attention_mask = inputs['attention_mask'][i]
                token_type_ids = inputs['token_type_ids'][i]
                offsets = offset_mapping[i]
                sample_idx = sample_mapping[i]
                cls_index = input_ids.index(tokenizer.cls_token_id)
                sequence_ids = inputs.sequence_ids(i)
                context_start = sequence_ids.index(1)
                context_end = len(sequence_ids) - sequence_ids[::-1].index(1)
                
                if not (start_char >= offsets[context_start][0] and end_char <= offsets[context_end - 1][1]):
                    start_positions = cls_index
                    end_positions = cls_index
                else:
                    start_positions = end_positions = None
                    for idx, (offset_start, offset_end) in enumerate(offsets):
                        if offset_start == offset_end:
                            continue
                        if offset_start <= start_char and offset_end >= start_char:
                            start_positions = idx
                        if offset_start <= end_char and offset_end >= end_char:
                            end_positions = idx
                    if start_positions is None:
                        start_positions = cls_index
                    if end_positions is None:
                        end_positions = cls_index
                self.examples.append({
                    'input_ids': torch.tensor(input_ids),
                    'attention_mask': torch.tensor(attention_mask),
                    'token_type_ids': torch.tensor(token_type_ids),
                    'start_positions': torch.tensor(start_positions),
                    'end_positions': torch.tensor(end_positions)
                })

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        return self.examples[idx]

# Initialize tokenizer and models
tokenizer = BertTokenizerFast.from_pretrained('bert-base-uncased')
teacher_model = BertForQuestionAnswering.from_pretrained('bert-large-uncased').to(device)
student_model = BertForQuestionAnswering.from_pretrained('bert-base-uncased').to(device)

# Create datasets and dataloaders
train_dataset = SquadDataset(train_data, tokenizer)
train_loader = DataLoader(train_dataset, batch_size=8, shuffle=True)

# Set up optimizer
optimizer = AdamW(student_model.parameters(), lr=3e-5)

# Define knowledge distillation loss
def distillation_loss(teacher_outputs, student_outputs, start_positions, end_positions, alpha=0.5, temperature=2.0):
    start_logits_teacher, end_logits_teacher = teacher_outputs.start_logits, teacher_outputs.end_logits
    start_logits_student, end_logits_student = student_outputs.start_logits, student_outputs.end_logits
    
    # Cross-entropy loss with ground-truth positions
    ce_loss = F.cross_entropy(start_logits_student, start_positions) + F.cross_entropy(end_logits_student, end_positions)
    
    # KL divergence loss between teacher and student logits (soft target)
    kl_loss_start = F.kl_div(
        F.log_softmax(start_logits_student / temperature, dim=-1),
        F.softmax(start_logits_teacher / temperature, dim=-1),
        reduction='batchmean'
    )
    kl_loss_end = F.kl_div(
        F.log_softmax(end_logits_student / temperature, dim=-1),
        F.softmax(end_logits_teacher / temperature, dim=-1),
        reduction='batchmean'
    )
    kl_loss = kl_loss_start + kl_loss_end
    loss = alpha * ce_loss + (1 - alpha) * kl_loss * (temperature ** 2)
    return loss

# Initialize the minimum loss variable
min_loss = float("inf")

# Training loop with knowledge distillation
epochs = 3
for epoch in range(epochs):
    print(f"Epoch {epoch + 1}/{epochs}")
    student_model.train()
    teacher_model.eval()  # Freeze the teacher model
    
    for batch in tqdm(train_loader, leave=True):
        input_ids = batch['input_ids'].to(device)
        attention_mask = batch['attention_mask'].to(device)
        token_type_ids = batch['token_type_ids'].to(device)
        start_positions = batch['start_positions'].to(device)
        end_positions = batch['end_positions'].to(device)
        
        # Get teacher's output
        with torch.no_grad():
            teacher_outputs = teacher_model(
                input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids
            )
        
        # Get student's output
        student_outputs = student_model(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            start_positions=start_positions,
            end_positions=end_positions
        )
        
        # Calculate distillation loss
        loss = distillation_loss(
            teacher_outputs, 
            student_outputs, 
            start_positions, 
            end_positions, 
            alpha=0.5, 
            temperature=2.0
        )
        
        # Backpropagation
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        
        # Print loss for tracking
        current_loss = loss.item()
        tqdm.write(f"Loss: {current_loss:.4f}")
        
        # Save model if this is the smallest loss so far
        if current_loss < min_loss:
            min_loss = current_loss
            # Save the fine-tuned student model
            save_directory = '../models/distilled_model'
            if not os.path.exists(save_directory):
                os.makedirs(save_directory)
            student_model.save_pretrained(save_directory)
            tokenizer.save_pretrained(save_directory)
            print(f"New best model saved with loss {min_loss:.4f} at {save_directory}")

print(f"Training complete. Best model saved with loss {min_loss:.4f}.")

# Save the fine-tuned student model
student_model.save_pretrained(save_directory)
tokenizer.save_pretrained(save_directory)
print("Distilled model and tokenizer saved.")

# prediction code
student_model.eval()
predictions = collections.OrderedDict()
with torch.no_grad():
    for item in tqdm(dev_data):
        inputs = tokenizer(
            item['question'],
            item['context'],
            max_length=384,
            truncation='only_second',
            stride=128,
            return_overflowing_tokens=True,
            return_offsets_mapping=True,
            padding='max_length',
            return_tensors='pt'
        )
        input_ids = inputs['input_ids'].to(device)
        attention_mask = inputs['attention_mask'].to(device)
        token_type_ids = inputs['token_type_ids'].to(device)
        outputs = student_model(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids
        )
        start_logits = outputs.start_logits
        end_logits = outputs.end_logits
        offset_mapping = inputs['offset_mapping']
        sample_mapping = inputs['overflow_to_sample_mapping']

        # For each input
        for i in range(len(input_ids)):
            # Offset mappings
            offsets = offset_mapping[i]
            # Get the most probable start and end of answer span
            start_logit = start_logits[i]
            end_logit = end_logits[i]
            # Convert to probabilities
            start_indexes = torch.argsort(start_logit, descending=True)[:20]
            end_indexes = torch.argsort(end_logit, descending=True)[:20]
            # Generate possible answer spans
            context = item['context']
            valid_answers = []
            for start_index in start_indexes:
                for end_index in end_indexes:
                    if start_index >= len(offsets) or end_index >= len(offsets):
                        continue
                    if offsets[start_index] is None or offsets[end_index] is None:
                        continue
                    if offsets[start_index][0] > offsets[end_index][1]:
                        continue
                    answer = context[offsets[start_index][0]:offsets[end_index][1]]
                    valid_answers.append({
                        'text': answer,
                        'score': start_logit[start_index] + end_logit[end_index]
                    })
            if len(valid_answers) > 0:
                best_answer = sorted(valid_answers, key=lambda x: x['score'], reverse=True)[0]['text']
            else:
                best_answer = ''
            predictions[item['qid']] = best_answer

# Save predictions to a JSON file
with open('../eval/distilled_predictions.json', 'w') as f:
    json.dump(predictions, f)

print('Predictions saved to distilled_predictions.json')