"""Network limits for GDAL reads of remote rasters over HTTP.

GDAL has no HTTP timeout by default: a server that accepts the connection and never answers holds the
reading thread for ever. Sage tools read remote COGs in threads under a deadline, and an abandoned
thread still occupies one of the few worker threads the whole app shares, so every remote read in a
Sage tool path opens under these limits (2026-10-07: get_insurance_intelligence ran past 120 s).

They are scoped to these reads, not set for the whole process: a process-wide GDAL_HTTP_TIMEOUT would
also cut long but progressing downloads, such as ogr2ogr importing a large remote file over /vsicurl.
"""

# Seconds: to connect, and for one HTTP request in all. A COG read is a few small range requests;
# a healthy one takes well under a second each (WaPOR, Planetary Computer, Earth Search).
GDAL_HTTP_TIMEOUTS = {
    "GDAL_HTTP_CONNECTTIMEOUT": "10",
    "GDAL_HTTP_TIMEOUT": "30",
}
