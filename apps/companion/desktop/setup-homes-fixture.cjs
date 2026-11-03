'use strict';
// Technical receipts only, for callers that supply an isolated test directory.
const fs=require('node:fs/promises'),path=require('node:path');
async function installedSetup(directory, id='b'.repeat(32)) {
  const root=path.join(directory,'hosts/hermes');
  const attempt=path.join(root,'installations',id);
  const host={schema:'daemon.companion-host-install.v1',installation_id:id,version:'0.21.5',revision:'c'.repeat(40)};
  const receipt={schema:'daemon.private-setup.v1',username:'SyntheticDaemon_bot',user_id:'12345',transaction_id:'d'.repeat(32)};
  await fs.mkdir(path.join(attempt,'profile'),{recursive:true});
  await fs.mkdir(path.join(attempt,'daemon'),{recursive:true});
  const files={pointer:path.join(root,'current.json'),owner:path.join(attempt,'owner.json'),receipt:path.join(attempt,'daemon-setup.json'),scope:path.join(attempt,'daemon/.daemon-harness-installation.json')};
  for(const [name,data] of Object.entries({pointer:host,owner:host,receipt,scope:{schema:'daemon.companion-installation-scope.v1',host}}))
    await fs.writeFile(files[name],JSON.stringify(data)+'\n');
  return {attempt,files,host,receipt};
}
module.exports={installedSetup};
