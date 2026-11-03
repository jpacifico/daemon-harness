/* Group only a verified common runtime, never a matching display name. */
'use strict';
function groupDaemons(rows) {
  const groups=new Map(),hosts=[];
  for(const row of rows) {
    if(!row.daemon){hosts.push(row);continue;}
    const d=row.daemon,key=d.id&&d.runtime?d.id+':'+d.runtime:'host:'+row.id;
    if(!groups.has(key))groups.set(key,{id:row.id,name:d.name||'',hosts:[]});
    groups.get(key).hosts.push(row);
  }
  return {daemons:[...groups.values()],hosts};
}
if(typeof module!=='undefined')module.exports={groupDaemons};
