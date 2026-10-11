# Data

The datasets and run outputs of the paper. The results were obtained with `unsloth/Qwen3.5-397B-A17B-GGUF:Q4_K_M` served by llama.cpp on the evaluation sets `data/scenarios/<taxonomy>/eval.jsonl`, with `data/scenarios/<taxonomy>/examples.jsonl` as example pool and the pipeline in `ablation` mode.

| Path | Content |
|---|---|
| `scenarios/<taxonomy>/scenarios.json` | Scenario set the requests were generated from. |
| `scenarios/<taxonomy>/generation_metadata.json` | Generation settings and validation statistics of each scenario. |
| `scenarios/<taxonomy>/eval.jsonl` | Evaluation set. |
| `scenarios/<taxonomy>/examples.jsonl` | Example pool. |
| `annotations/<taxonomy>/output/<checkpoint>/` | Output of each pipeline step in the runs of the paper. |
| `annotations/<taxonomy>/output/evaluation/` | Evaluation report and list of false negatives of each checkpoint. |

The reports were recomputed from the step outputs with the code in this repository; see the README for the commands. The step outputs were produced by an earlier version of the pipeline and contain no message identifiers.

## Results

In these runs, the evaluation sets of mHealth and PlayStore were erroneously read as MacRoman instead of UTF-8, which garbled all characters outside ASCII in 140 of 926 mHealth and 49 of 982 PlayStore message bodies before they reached the model. Ground truth was read in the same way, so the comparison is consistent. Recomputing the reports of these two sets therefore requires `-input-encoding mac-roman`; see "Differences from the paper" in the README. The tables show micro-averaged precision (P), recall (R) and F1 at label level and at instance level with fuzzy and exact matching, and correspond to Tables 1 and 2 of the paper.

## AI4Privacy

| Step | Label-Level P | Label-Level R | Label-Level F1 | Fuzzy Instance P | Fuzzy Instance R | Fuzzy Instance F1 | Exact Instance P | Exact Instance R | Exact Instance F1 |
|------|---------------|---------------|----------------|------------------|------------------|-------------------|------------------|------------------|-------------------|
| Classifier | 97.79% | 99.42% | 98.60% | - | - | - | - | - | - |
| Annotator (single-stage) | 98.38% | 98.77% | 98.57% | 96.71% | 97.69% | 97.20% | 93.29% | 94.25% | 93.77% |
| Annotator (two-stage) | 98.36% | 99.42% | 98.88% | 96.85% | 98.69% | 97.76% | 93.48% | 95.29% | 94.38% |
| Reviewer (single-stage) | 97.97% | 99.14% | 98.55% | 95.83% | 97.66% | 96.74% | 92.31% | 94.09% | 93.19% |
| Reviewer (two-stage) | 98.12% | 99.42% | 98.76% | 96.50% | 98.45% | 97.47% | 93.06% | 94.96% | 94.00% |

## mHealth

| Step | Label-Level P | Label-Level R | Label-Level F1 | Fuzzy Instance P | Fuzzy Instance R | Fuzzy Instance F1 | Exact Instance P | Exact Instance R | Exact Instance F1 |
|------|---------------|---------------|----------------|------------------|------------------|-------------------|------------------|------------------|-------------------|
| Classifier | 97.81% | 99.36% | 98.58% | - | - | - | - | - | - |
| Annotator (single-stage) | 98.13% | 98.72% | 98.43% | 95.52% | 96.35% | 95.93% | 95.11% | 95.87% | 95.49% |
| Annotator (two-stage) | 98.24% | 99.38% | 98.80% | 96.27% | 97.81% | 97.03% | 95.84% | 97.33% | 96.58% |
| Reviewer (single-stage) | 98.34% | 99.07% | 98.70% | 95.72% | 96.27% | 95.99% | 95.22% | 95.74% | 95.48% |
| Reviewer (two-stage) | 98.41% | 99.22% | 98.81% | 96.12% | 96.82% | 96.47% | 95.63% | 96.32% | 95.97% |

## PlayStore

| Step | Label-Level P | Label-Level R | Label-Level F1 | Fuzzy Instance P | Fuzzy Instance R | Fuzzy Instance F1 | Exact Instance P | Exact Instance R | Exact Instance F1 |
|------|---------------|---------------|----------------|------------------|------------------|-------------------|------------------|------------------|-------------------|
| Classifier | 87.18% | 95.38% | 91.09% | - | - | - | - | - | - |
| Annotator (single-stage) | 90.44% | 91.38% | 90.91% | 85.76% | 86.91% | 86.33% | 80.01% | 81.21% | 80.61% |
| Annotator (two-stage) | 88.99% | 94.88% | 91.84% | 82.85% | 89.95% | 86.25% | 76.98% | 83.54% | 80.12% |
| Reviewer (single-stage) | 88.63% | 92.51% | 90.53% | 83.70% | 87.80% | 85.70% | 78.22% | 82.22% | 80.17% |
| Reviewer (two-stage) | 87.55% | 94.25% | 90.78% | 81.96% | 89.45% | 85.54% | 76.33% | 83.46% | 79.73% |

