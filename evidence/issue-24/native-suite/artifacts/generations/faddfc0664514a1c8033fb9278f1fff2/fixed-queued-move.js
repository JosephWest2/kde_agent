var id="bffe99ed-c8f2-49f3-98db-37ec8f36257f";
var ws=workspace.windowList(); var moved=false;
for(var i=0;i<ws.length && i<256;i++){var w=ws[i];if(String(w.internalId).replace(/[{}]/g,"").toLowerCase()===id){var r=w.frameGeometry; r.x=r.x+50; r.y=r.y+30; w.frameGeometry=r; moved=true; break;}}
output_result(JSON.stringify({moved:moved}));
