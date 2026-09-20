var id="a6041aec-4eff-406e-88b8-99617d632f3a";
var ws=workspace.windowList(); var moved=false;
for(var i=0;i<ws.length && i<256;i++){var w=ws[i];if(String(w.internalId).replace(/[{}]/g,"").toLowerCase()===id){var r=w.frameGeometry; r.x=r.x+50; r.y=r.y+30; w.frameGeometry=r; moved=true; break;}}
output_result(JSON.stringify({moved:moved}));
