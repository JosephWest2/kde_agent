// Fixed installed query. Only the request identity is encoded data.
function nullable(value) { return value === undefined || value === null ? null : value; }
function rectangle(value) {
    if (value == null) return null;
    var keys = ['x', 'y', 'width', 'height'];
    // JSON.stringify otherwise silently converts NaN/Infinity into valid nulls.
    if (keys.some(k => value[k] != null && (typeof value[k] !== 'number' || !Number.isFinite(value[k]))))
        return {invalid: true};
    if (keys.some(k => value[k] == null)) return null;
    return {x:value.x, y:value.y, width:value.width, height:value.height};
}
var active = workspace.activeWindow;
var activeId = active == null ? null : nullable(active.internalId);
if (activeId !== null) activeId = String(activeId);
output_result(JSON.stringify({schema_version:1, request_id:request.request_id,
    active_uuid:activeId,
    outputs:workspace.screens.map(o => ({name:o.name, width:o.geometry.width, height:o.geometry.height, scale:nullable(o.scale)})),
    windows:workspace.windowList().map(w => ({uuid:w.internalId == null ? null : String(w.internalId),
        pid:w.pid == null || w.pid === 0 ? null : w.pid,
        title:nullable(w.caption), class:nullable(w.resourceClass),
        client:rectangle(w.clientGeometry), frame:rectangle(w.frameGeometry),
        active:activeId !== null && String(w.internalId) === activeId}))}));
