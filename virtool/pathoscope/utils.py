import os
import visvalingamwyatt as vw


def coverage_to_coordinates(coverage_list):
    previous_depth = coverage_list[0]
    coordinates = {(0, previous_depth)}

    last = len(coverage_list) - 1

    for i, depth in enumerate(coverage_list):
        if depth != previous_depth or i == last:
            coordinates.add((i - 1, previous_depth))
            coordinates.add((i, depth))

            previous_depth = depth

    coordinates = sorted(list(coordinates), key=lambda x: x[0])

    if len(coordinates) > 100:
        return vw.simplify(coordinates, ratio=0.4)

    return coordinates


def get_pathoscope_json_path(data_path, analysis_id, sample_id):
    return os.path.join(
        data_path,
        "samples",
        sample_id,
        "analysis",
        analysis_id,
        "pathoscope.json"
    )
