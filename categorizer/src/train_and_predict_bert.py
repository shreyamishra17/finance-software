#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Tuple

import numpy as np
import pandas as pd
import torch
from sklearn.model_selection import train_test_split
from torch.optim import AdamW
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModelForSequenceClassification, AutoTokenizer


class TextDataset(Dataset):
    def __init__(self, encodings: dict, labels: np.ndarray | None = None):
        self.encodings = encodings
        self.labels = labels

    def __len__(self) -> int:
        return len(self.encodings["input_ids"])

    def __getitem__(self, idx: int) -> dict:
        item = {
            "input_ids": self.encodings["input_ids"][idx],
            "attention_mask": self.encodings["attention_mask"][idx],
        }
        if self.labels is not None:
            item["labels"] = torch.tensor(self.labels[idx], dtype=torch.long)
        return item


def load_training_data(simulated_csv: Path, cred_csv: Path) -> pd.DataFrame:
    sim_df = pd.read_csv(simulated_csv, usecols=["raw_string", "category"]).copy()
    sim_df.columns = ["text", "label"]

    cred_df = pd.read_csv(cred_csv, usecols=["string", "Category"]).copy()
    cred_df.columns = ["text", "label"]

    df = pd.concat([sim_df, cred_df], ignore_index=True)
    df["text"] = df["text"].fillna("").astype(str).str.strip()
    df["label"] = df["label"].fillna("").astype(str).str.strip()
    df = df[(df["text"] != "") & (df["label"] != "")]
    return df.reset_index(drop=True)


def build_label_maps(labels: pd.Series) -> Tuple[dict, dict]:
    classes = sorted(labels.unique().tolist())
    label2id = {label: i for i, label in enumerate(classes)}
    id2label = {i: label for label, i in label2id.items()}
    return label2id, id2label


def encode_texts(tokenizer, texts: list[str], max_length: int) -> dict:
    encoded = tokenizer(
        texts,
        truncation=True,
        padding=True,
        max_length=max_length,
        return_tensors="pt",
    )
    return {
        "input_ids": encoded["input_ids"],
        "attention_mask": encoded["attention_mask"],
    }


def run_epoch(model, dataloader, optimizer, device: torch.device) -> float:
    model.train()
    losses = []
    for step, batch in enumerate(dataloader, start=1):
        batch = {k: v.to(device) for k, v in batch.items()}
        outputs = model(**batch)
        loss = outputs.loss
        loss.backward()
        optimizer.step()
        optimizer.zero_grad()
        losses.append(loss.item())
        if step % 25 == 0:
            print(f"  step {step}/{len(dataloader)} loss={loss.item():.4f}")
    return float(np.mean(losses)) if losses else 0.0


@torch.no_grad()
def evaluate(model, dataloader, device: torch.device) -> float:
    model.eval()
    correct = 0
    total = 0
    for batch in dataloader:
        labels = batch["labels"].to(device)
        inputs = {
            "input_ids": batch["input_ids"].to(device),
            "attention_mask": batch["attention_mask"].to(device),
        }
        logits = model(**inputs).logits
        preds = logits.argmax(dim=1)
        correct += (preds == labels).sum().item()
        total += labels.size(0)
    return (correct / total) if total > 0 else 0.0


@torch.no_grad()
def predict_with_scores(model, dataloader, id2label: dict, device: torch.device) -> Tuple[list[str], list[float]]:
    model.eval()
    labels = []
    scores = []
    for batch in dataloader:
        inputs = {
            "input_ids": batch["input_ids"].to(device),
            "attention_mask": batch["attention_mask"].to(device),
        }
        logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=1)
        conf, idx = probs.max(dim=1)
        labels.extend([id2label[int(i)] for i in idx.cpu().numpy()])
        scores.extend(conf.cpu().numpy().tolist())
    return labels, scores


def select_device() -> torch.device:
    """Prefer Apple Silicon GPU (MPS), then CUDA, then CPU."""
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def main() -> None:
    parser = argparse.ArgumentParser(description="Train a BERT classifier and predict categories.")
    parser.add_argument("--simulated-csv", type=Path, default=Path("categorizer/datasets/simulated_data_large.csv"))
    parser.add_argument("--cred-csv", type=Path, default=Path("categorizer/datasets/cred_cat_filled.csv"))
    parser.add_argument(
        "--input-transactions",
        type=Path,
        default=Path("my_finance/processed/2025-03-12_to_2025-04-10_transactions.csv"),
    )
    parser.add_argument(
        "--output-predictions",
        type=Path,
        default=Path("categorizer/bert_predictions_2025-03-12_to_2025-04-10.csv"),
    )
    parser.add_argument("--model-name", default="prajjwal1/bert-tiny")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--max-length", type=int, default=64)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--save-dir", type=Path, default=Path("categorizer/models/bert_tiny_transactions"))
    parser.add_argument("--train-limit", type=int, default=0, help="If > 0, train on first N rows after shuffle.")
    parser.add_argument(
        "--freeze-base",
        action="store_true",
        help="Freeze base transformer and train only classification head for faster CPU runs.",
    )
    args = parser.parse_args()

    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    df = load_training_data(args.simulated_csv, args.cred_csv)
    df = df.sample(frac=1.0, random_state=args.seed).reset_index(drop=True)
    if args.train_limit > 0:
        df = df.head(args.train_limit).copy()

    label2id, id2label = build_label_maps(df["label"])
    df["label_id"] = df["label"].map(label2id)

    label_counts = df["label"].value_counts()
    do_stratify = label_counts.min() >= 2

    train_df, val_df = train_test_split(
        df,
        test_size=0.2,
        random_state=args.seed,
        stratify=df["label_id"] if do_stratify else None,
    )

    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.model_name,
        num_labels=len(label2id),
        id2label={int(k): v for k, v in id2label.items()},
        label2id=label2id,
    )
    if args.freeze_base:
        base = getattr(model, model.base_model_prefix, None)
        if base is not None:
            for p in base.parameters():
                p.requires_grad = False

    train_enc = encode_texts(tokenizer, train_df["text"].tolist(), args.max_length)
    val_enc = encode_texts(tokenizer, val_df["text"].tolist(), args.max_length)

    train_ds = TextDataset(train_enc, train_df["label_id"].to_numpy())
    val_ds = TextDataset(val_enc, val_df["label_id"].to_numpy())

    train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False)

    device = select_device()
    print(f"Training BERT on: {device}")
    model.to(device)
    optimizer = AdamW(model.parameters(), lr=args.lr)

    for epoch in range(1, args.epochs + 1):
        train_loss = run_epoch(model, train_loader, optimizer, device)
        val_acc = evaluate(model, val_loader, device)
        print(f"Epoch {epoch}/{args.epochs} - train_loss={train_loss:.4f} val_acc={val_acc:.4f}")

    args.save_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(args.save_dir)
    tokenizer.save_pretrained(args.save_dir)

    meta = {
        "model_name": args.model_name,
        "num_labels": len(label2id),
        "labels": sorted(label2id.keys()),
        "train_rows": int(len(train_df)),
        "val_rows": int(len(val_df)),
        "epochs": args.epochs,
        "batch_size": args.batch_size,
        "max_length": args.max_length,
    }
    (args.save_dir / "training_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    tx_df = pd.read_csv(args.input_transactions)
    if "merchant_raw" not in tx_df.columns:
        raise ValueError("Expected 'merchant_raw' column in input transactions CSV.")

    tx_raw = tx_df["merchant_raw"].fillna("").astype(str)
    pred_enc = encode_texts(tokenizer, tx_raw.tolist(), args.max_length)
    pred_ds = TextDataset(pred_enc, labels=None)
    pred_loader = DataLoader(pred_ds, batch_size=args.batch_size, shuffle=False)

    pred_labels, pred_scores = predict_with_scores(model, pred_loader, id2label, device)

    out_df = pd.DataFrame(
        {
            "raw_string": tx_raw,
            "bert_category": pred_labels,
            "bert_score": [round(float(s), 4) for s in pred_scores],
        }
    )
    args.output_predictions.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(args.output_predictions, index=False)

    print(f"Saved model to: {args.save_dir}")
    print(f"Saved predictions to: {args.output_predictions}")
    print(f"Predicted rows: {len(out_df)}")


if __name__ == "__main__":
    main()
