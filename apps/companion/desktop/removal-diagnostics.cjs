"use strict";
const STAGES = new Set(['inventory','related_problem','shared_memory','host_registration','nested_installation','task_capture','dependency_scan','scheduler_connect','decode_arguments','folder_scan','task_xml','task_shape','task_owner','task_privileges','task_command','task_directory','stop_services','stop_processes','restore_config','remove_plugins','restart_host','save_state','cleanup_services','cleanup_processes','program_identity','program_delete','detach_host','remove_added_hosts','file_open','file_identity','file_attributes','file_delete','file_ancestor','file_limit','rollback','transport','unknown']);
const CODES = new Set(['services','platform','ownership','shared','changed','request','memory','unsafe','configuration','followups','recovery','unconfirmed','attached']);
function details(value) {
  const result={stage:STAGES.has(value?.stage)?value.stage:'unknown'};
  for(const key of ['hresult','exit_code','errno','winerror'])if(Number.isInteger(value?.[key])&&value[key]>=-2147483648&&value[key]<=2147483647)result[key]=value[key];
  // The task or launchd label that still uses the daemon's files: plain characters only.
  if(typeof value?.blocking_task==='string'&&/^[A-Za-z0-9 ._\\()-]{1,200}$/.test(value.blocking_task))result.blocking_task=value.blocking_task;
  return result;
}
function blocked(error, phase, metadata={}) {
  const candidate=/^removal\.([a-z]+)$/.exec(error?.message||'')?.[1];
  const code=CODES.has(candidate)?candidate:'unconfirmed';
  const diagnostic={schema:'daemon.removal-diagnostic.v1',
    revision:/^[a-f0-9]{7,64}$/.test(metadata.revision||'')?metadata.revision:'unknown',
    platform:['darwin','win32'].includes(process.platform)?process.platform:'unknown',
    at:new Date().toISOString(),phase:phase==='apply'?'apply':'review',code,
    ...details(error?.removalDiagnostic)};
  return {blocked:true,code:'removal.'+code,diagnostic};
}
module.exports={details,blocked};
