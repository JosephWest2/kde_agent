var id="9b45031b-ee19-48c7-ba04-abf1e84252db";
var ws=workspace.windowList(); var moved=false;
for(var i=0;i<ws.length && i<256;i++){var w=ws[i];if(String(w.internalId).replace(/[{}]/g,"").toLowerCase()===id){var r=Object.assign({},w.frameGeometry); r.x=r.x+50; r.y=r.y+30; w.frameGeometry=r; moved=true; break;}}
output_result(JSON.stringify({moved:moved}));
