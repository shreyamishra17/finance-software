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


def build_label_maps(labels: pd.Series) -> Tuple[dict[str, int], dict[int, str]]:
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
    for batch in dataloader:
        batch = {k: v.to(device) for k, v in batch.items()}
        outputs = model(**batch)
        loss = outputs.loss
        loss.backward()
        optimizer.step()
        optimizer.zero_grad()
        losses.append(loss.item())
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
def predict_with_scores(model, dataloader, id2label: dict[int, str], device: torch.device) -> tuple[list[str], list[float]]:
    model.eval()
    pred_labels: list[str] = []
    pred_scores: list[float] = []
    for batch in dataloader:
        inputs = {
            "input_ids": batch["input_ids"].to(device),
            "attention_mask": batch["attention_mask"].to(device),
        }
        logits = model(**inputs).logits
        probs = torch.softmax(logits, dim=1)
        conf, idx = probs.max(dim=1)
        pred_labels.extend([id2label[int(i)] for i in idx.cpu().numpy()])
        pred_scores.extend(conf.cpu().numpy().tolist())
    return pred_labels, pred_scores


def select_device() -> torch.device:
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train DistilBERT on merchant/category CSV and run predictions on input CSV."
    )
    parser.add_argument(
        "--train-csv",
        type=Path,
        default=Path("categorizer/datasets/training_companies_for_bert.csv"),
    )
    parser.add_argument("--train-text-col", default="Merchant")
    parser.add_argument("--train-label-col", default="Category")
    parser.add_argument(
        "--input-csv",
        type=Path,
        default=Path("my_finance/processed/2025-03-12_to_2025-04-10_transactions.csv"),
    )
    parser.add_argument("--input-text-col", default="merchant_clean")
    parser.add_argument(
        "--output-predictions",
        type=Path,
        default=Path("categorizer/datasets/merchant_clean_distilbert_categories.csv"),
    )
    parser.add_argument("--model-name", default="distilbert-base-uncased")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--max-length", type=int, default=48)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--test-size", type=float, default=0.2)
    parser.add_argument(
        "--save-dir",
        type=Path,
        default=Path("categorizer/models/distilbert_companies"),
    )
    parser.add_argument(
        "--freeze-base",
        action="store_true",
        help="Freeze the DistilBERT base and train only classifier head.",
    )
    args = parser.parse_args()

    if not args.train_csv.exists():
        raise FileNotFoundError(f"Training CSV not found: {args.train_csv}")
    if not args.input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {args.input_csv}")

    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    df = pd.read_csv(args.train_csv)
    needed = {args.train_text_col, args.train_label_col}
    if not needed.issubset(df.columns):
        raise ValueError(f"Training CSV must contain {needed}. Found: {set(df.columns)}")
    df = df[[args.train_text_col, args.train_label_col]].copy()
    df.columns = ["text", "label"]
    df["text"] = df["text"].fillna("").astype(str).str.strip()
    df["label"] = df["label"].fillna("").astype(str).str.strip()
    df = df[(df["text"] != "") & (df["label"] != "")]
    if df.empty:
        raise ValueError("No valid training rows.")

    label2id, id2label = build_label_maps(df["label"])
    df["label_id"] = df["label"].map(label2id)

    train_df, val_df = train_test_split(
        df,
        test_size=args.test_size,
        random_state=args.seed,
        stratify=df["label_id"],
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
    print(f"Training DistilBERT on: {device}")
    model.to(device)
    optimizer = AdamW(model.parameters(), lr=args.lr)

    history: list[dict[str, float]] = []
    for epoch in range(1, args.epochs + 1):
        train_loss = run_epoch(model, train_loader, optimizer, device)
        val_acc = evaluate(model, val_loader, device)
        history.append({"epoch": epoch, "train_loss": train_loss, "val_acc": val_acc})
        print(f"Epoch {epoch}/{args.epochs} - train_loss={train_loss:.4f} val_acc={val_acc:.4f}")

    args.save_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(args.save_dir)
    tokenizer.save_pretrained(args.save_dir)
    (args.save_dir / "training_meta.json").write_text(
        json.dumps(
            {
                "model_name": args.model_name,
                "num_labels": len(label2id),
                "labels": sorted(label2id.keys()),
                "train_rows": int(len(train_df)),
                "val_rows": int(len(val_df)),
                "epochs": args.epochs,
                "batch_size": args.batch_size,
                "max_length": args.max_length,
                "history": history,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    tx_df = pd.read_csv(args.input_csv)
    if args.input_text_col not in tx_df.columns:
        raise ValueError(f"Input CSV must contain '{args.input_text_col}'")
    tx_text = tx_df[args.input_text_col].fillna("").astype(str)
    pred_enc = encode_texts(tokenizer, tx_text.tolist(), args.max_length)
    pred_ds = TextDataset(pred_enc, labels=None)
    pred_loader = DataLoader(pred_ds, batch_size=args.batch_size, shuffle=False)
    pred_labels, pred_scores = predict_with_scores(model, pred_loader, id2label, device)

    out_df = pd.DataFrame(
        {
            args.input_text_col: tx_text,
            "distilbert_category": pred_labels,
            "distilbert_score": [round(float(s), 6) for s in pred_scores],
        }
    )
    args.output_predictions.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(args.output_predictions, index=False)

    print(f"Saved model to: {args.save_dir}")
    print(f"Saved predictions to: {args.output_predictions}")
    print(f"Predicted rows: {len(out_df)}")


if __name__ == "__main__":
    main()
