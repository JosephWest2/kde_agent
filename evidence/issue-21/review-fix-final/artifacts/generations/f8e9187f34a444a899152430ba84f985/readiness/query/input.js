const request = {"request_id": "readiness-f8e9187f34a444a899152430ba84f985"};
// Fixed feasibility query. `request` is an encoded data object, never caller code.
function nullable(value) {
    return value === undefined || value === null ? null : value;
}
function rectangle(value) {
    if (value === undefined || value === null) return null;
    return {x: nullable(value.x), y: nullable(value.y),
            width: nullable(value.width), height: nullable(value.height)};
}
function metadata(w, active) {
    return {uuid: w.internalId == null ? null : String(w.internalId),
            pid: typeof w.pid === "number" && w.pid > 0 ? w.pid : null,
            title: nullable(w.caption), class: nullable(w.resourceClass),
            client: rectangle(w.clientGeometry), frame: rectangle(w.frameGeometry),
            active: active == null ? false : String(w.internalId) === active};
}
if (request.check_data) {
    output_result(JSON.stringify({echo: request.echo, unavailable: metadata({}, null)}));
} else {
    var active = workspace.activeWindow;
    var activeId = active == null ? null : String(active.internalId);
    output_result(JSON.stringify({schema_version: 1, request_id: request.request_id,
        active_uuid: activeId, outputs: workspace.screens.map(o => ({name: o.name, width: o.geometry.width, height: o.geometry.height, scale: o.scale})),
        windows: workspace.windowList().map(w => metadata(w, activeId))
                                      .sort((a, b) => a.uuid.localeCompare(b.uuid))}));
}
