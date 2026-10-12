/* Configurable workflows are playbooks; policy still owns permission. */
(function (root) {
  'use strict';
  function definition(template, fields) {
    const result = JSON.parse(JSON.stringify(template));
    for (const name of ['id', 'version', 'name', 'purpose']) result[name] = String(fields[name] || '').trim();
    for (const [name, max] of [['max_queries', 3], ['max_findings', 4], ['max_limitations', 3]]) {
      const value = Number(fields[name]);
      if (!Number.isInteger(value) || value < 1 || value > max) throw new Error(`${name} must be an integer from 1 to ${max}.`);
      result.workflow[name] = value;
    }
    result.workflow.follow_up_queries = fields.follow_up_queries === true;
    result.egress = fields.external_model === true ? 'allowlisted' : 'forbidden';
    return result;
  }
  function policyFields(policy) {
    const limits = policy.obligations || {};
    return {
      'Default decision': policy.default === 'deny' ? 'Deny unless explicitly permitted' : 'Unavailable',
      'Maximum rows per query': limits.max_rows ?? 'Unavailable',
      'Query timeout': limits.timeout_ms == null ? 'Unavailable' : `${limits.timeout_ms} ms`,
      'Permitted schemas': (limits.allowed_schemas || []).join(', ') || 'No schemas permitted',
      'Deployment table ceiling': (limits.allowed_tables || []).join(', ') || 'Exact tables are selected and approved in Sources',
      'External model permission': limits.allow_external_egress === true ? 'May be allowed; source and playbook must also permit it' : 'Check the deployment egress policy; this view does not grant permission',
    };
  }
  const api = { definition, policyFields };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.OpsGraphWorkflows = api;
})(typeof window !== 'undefined' ? window : this);
