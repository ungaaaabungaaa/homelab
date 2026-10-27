'use strict';
const ids=['free','used','load','capacity','updated'];
const text=(id,value)=>{document.getElementById(id).textContent=value;};
const bytes=value=>`${(value/1024**3).toFixed(2)} GiB`;
let pending=false;
async function refresh(){
 if(pending)return;pending=true;
 const button=document.getElementById('refresh');button.disabled=true;
 try{
  const response=await fetch('/api/history',{cache:'no-store',signal:AbortSignal.timeout(5000)});
  if(!response.ok)throw Error('Readings unavailable.');
  const data=await response.json();const rows=data.samples;const row=rows.at(-1);
  if(!row)throw Error('No samples yet.');
  text('free',bytes(row.disk_free_bytes));text('used',`${(100*(1-row.disk_free_bytes/row.disk_total_bytes)).toFixed(1)}%`);
  text('capacity',`of ${bytes(row.disk_total_bytes)} total`);text('load',row.load1===null?'Unavailable':row.load1.toFixed(2));
  text('updated',new Date(row.timestamp_ms).toLocaleString());text('status',data.healthy?'Collector current':'Collector stale — check the service');
  document.getElementById('status').classList.toggle('error',!data.healthy);
  const start=rows[0].timestamp_ms,span=Math.max(1,row.timestamp_ms-start);
  const points=rows.map((item,index)=>`${rows.length===1?500:20+960*(item.timestamp_ms-start)/span},${220-200*item.disk_free_bytes/item.disk_total_bytes}`);
  document.getElementById('line').setAttribute('points',points.join(' '));
  text('history-description',`${rows.length} retained samples · nominal interval ${data.interval_seconds} seconds`);
 }catch{
  ids.forEach(id=>text(id,'—'));text('status','Cannot read monitor data. Check the local service.');
  document.getElementById('status').classList.add('error');document.getElementById('line').setAttribute('points','');
  text('history-description','History is unavailable.');
 }finally{pending=false;button.disabled=false;}
}
document.getElementById('refresh').addEventListener('click',refresh);refresh();setInterval(refresh,10000);
