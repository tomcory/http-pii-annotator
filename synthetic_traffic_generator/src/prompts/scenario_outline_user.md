# Scenario Planning Task

Plan the scenario set for the following PII label taxonomy.

## Label Taxonomy

{{LABELS_TAXONOMY}}

## Requirements

- **Number of scenarios**: exactly {{N_SCENARIOS}}
- **Scenario types**: {{TYPE_TARGETS}}
- **Coverage**: each of the {{N_LABELS}} labels is a request label of at least {{MIN_PER_LABEL}} scenario(s)
- **Balance**: no label is a request label of more than {{MAX_PER_LABEL}} scenarios
- **Focus**: at most {{MAX_LABELS}} request labels and at most {{MAX_RESPONSE_LABELS}} response labels per scenario

{{FEEDBACK}}

## Output

Return ONLY the JSON array of scenario objects as defined in the system prompt.
