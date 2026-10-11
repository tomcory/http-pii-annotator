Generate {{N_REQUESTS}} synthetic HTTP requests total.

Return ONLY a JSON array (no prose). Use ONLY the taxonomy labels provided in the previous message (exact string match).

Use the following scenario templates (request_profiles) and generate {{PER_SCENARIO}} requests for each listed scenario_id:
{{SCENARIO_ID_LIST_OR_COUNTS_JSON}}

{{SCENARIO_NOTES}}

<REQUEST_PROFILES_JSON>
{{REQUEST_PROFILES_JSON}}
</REQUEST_PROFILES_JSON>

Generate exactly one request for each of the following blueprints, in this order, and set the `blueprint` field of each request to the `blueprint` number it was generated from:

<BLUEPRINTS_JSON>
{{BLUEPRINTS_JSON}}
</BLUEPRINTS_JSON>

{{ALREADY_USED}}
