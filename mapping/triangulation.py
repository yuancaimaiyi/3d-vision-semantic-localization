import os
import shutil
import re
import subprocess
import numpy as np
import transforms3d as tf
from collections import namedtuple

import images
from colmap_database import COLMAPDatabase


ColmapCamera = namedtuple('ColmapCamera', ['model_id', 'width', 'height', 'params'])
ImagePose = namedtuple('ImagePose', ['position', 'orientation'])
Point3D = namedtuple('Point3D', ['point_id', 'x', 'y', 'z', 'r', 'g', 'b', 'error', 'point2d_list'])
MapLandmark = namedtuple('MapLandmark', ['x', 'y', 'z', 'sign_type', 'confidence_score'])


def colmap_camera_model_name(model_id):
    if model_id == 1:
        return 'PINHOLE'
    else:
        raise Exception ('Not implemented yet')


def get_camera_malaga_extract_07_right():
    model_id = 1 # PINHOLE camera model
    width = 1024
    height = 768
    fx = 795.11588
    fy = 795.11588
    cx = 517.12973
    cy = 395.59665
    params = np.array([fx, fy, cx, cy])
    return ColmapCamera(model_id=model_id, width=width, height=height, params=params)


# Calculates the pose of the camera from a given pose of the car.
def malaga_car_pose_to_camera_pose(position_car, orientation_car, right=True):
    if right:
        # Calculate pose of right camera

        # According to http://ingmec.ual.es/~jlblanco/papers/blanco2013malaga_urban_dataset_IJRR_draft.pdf
        camera_pitch = np.deg2rad(-8.2)

        camera_orientation_offset = tf.euler.euler2quat(-camera_pitch, 0, 0, 'rxyz')
        orientation_camera = tf.quaternions.qmult(orientation_car, camera_orientation_offset)
        # TODO Take positional offset into account as well
        return position_car, orientation_camera
    else:
        # Calculate pose of left camera
        # TODO Implement if needed
        raise Exception ('Not implemented yet')


def get_timestamps_from_images(image_names):
    # The timestamps are part of the file names of the images.
    # Examples:
    # img_CAMERA1_1261230001.080210_right.jpg
    # img_CAMERA1_1261230001.080210_left.jpg

    pattern = re.compile('img_CAMERA1_(\d*.\d*)_(right|left).jpg')

    result = []

    for name in image_names:
        match = pattern.match(name)
        timestamp_str = match.group(1)
        timestamp = float(timestamp_str)
        result.append(timestamp)
        assert(str(timestamp) in name)

    # There should be one timestamp for each image
    assert(len(result) == len(image_names))
    # Timestamps need to be monotonic
    assert(np.all(np.diff(np.array(result)) > 0))

    return result


def get_poses(gt_estimator, timestamps):
    result = []

    for timestamp in timestamps:
        # TODO Use 'kms' method, but needs to be fully implemented first
        position_car, orientation_car = gt_estimator.get_pose(timestamp, method='cubic')

        # The ground truth estimator gives the pose of the car, but we need the
        # pose of the camera.
        position_camera, orientation_camera = malaga_car_pose_to_camera_pose(position_car, orientation_car)

        # COLMAP wants the pose as a transformation from the world coordinate
        # frame to the camera coordinate frame The ground truth estimator gives
        # the pose of the car as a transformation from the car coordinate frame
        # to the world coordinate frame, so we need to invert it
        # Info on the camera coordinate system: https://colmap.github.io/format.html#images-txt
        orientation_inverted = tf.quaternions.qinverse(orientation_camera)
        position_inverted = -np.dot(tf.quaternions.quat2mat(orientation_inverted), position_camera)

        pose = ImagePose(orientation=orientation_inverted, position=position_inverted)
        result.append(pose)

    return result


def fill_database(database_path, camera, image_names, image_prior_poses, detections):
    # Open the database.
    db = COLMAPDatabase.connect(database_path)

    db.create_tables()

    camera_id = db.add_camera(camera.model_id, camera.width, camera.height, camera.params, prior_focal_length=True, camera_id=1)

    image_id = 0
    image_ids = {}
    keypoint_count = 0
    for image_name, prior_pose in zip(image_names, image_prior_poses):
        position = prior_pose.position
        orientation = prior_pose.orientation

        # Add image to database
        image_ids[image_name] = db.add_image(image_name, camera_id, prior_q=orientation, prior_t=position, image_id=image_id)

        # Add keypoints to database
        detections_in_image = detections[image_name]
        # Note that COLMAP supports:
        #      - 2D keypoints: (x, y)
        #      - 4D keypoints: (x, y, theta, scale)
        #      - 6D affine keypoints: (x, y, a_11, a_12, a_21, a_22)
        if len(detections_in_image) > 0:
            keypoints = np.array([[detection.x, detection.y] for detection in detections_in_image])
            db.add_keypoints(image_id, keypoints)
            keypoint_count += len(detections_in_image)

        # Matches will be imported later using the matches_importer
        #matches12 = np.array([[0, 0], [1, 1]])
        #db.add_matches(image_id1, image_id2, matches12)

        image_id += 1

    print(f'Added {image_id} images to COLMAP database.')
    print(f'Added {keypoint_count} keypoints to COLMAP database.')

    db.commit()
    db.close()

    return camera_id, image_ids


def write_matches_file(colmap_match_file_path, image_names, matches):
    # File format:
    # <image_name1> <image_name2>
    # <index image 1> <index image 2>
    # <index image 1> <index image 2>
    # ...
    # <blank line>
    # <image_name1> <image_name2>
    # ...

    with open(colmap_match_file_path, 'x') as f:
        image_pairs = set([tuple(sorted((m.image_idx1, m.image_idx2))) for m in matches])

        for image_pair in image_pairs:
            matches_between_pair = [m for m in matches if tuple(sorted((m.image_idx1, m.image_idx2))) == image_pair]

            image_name1 = image_names[image_pair[0]]
            image_name2 = image_names[image_pair[1]]
            f.write(f'{image_name1} {image_name2}\n')

            for match in matches_between_pair:
                f.write(f'{match.detection_idx1} {match.detection_idx2}\n')

            f.write('\n')


def fill_sparse_in_dir(colmap_sparse_input_path, camera, image_names, prior_poses, camera_id, image_ids):
    cameras_file = os.path.join(colmap_sparse_input_path, 'cameras.txt')
    images_file = os.path.join(colmap_sparse_input_path, 'images.txt')
    points3D_file = os.path.join(colmap_sparse_input_path, 'points3D.txt')

    # File format:
    # <camera id> <model> <width> <height> <param1> <param2> ...
    # ...
    #
    # Example for pinhole camera model:
    # <camera id> PINHOLE <width> <height> <fx> <fy> <cx> <cy>
    with open(cameras_file, 'x') as f:
        camera_model_name = colmap_camera_model_name(camera.model_id)
        param_list = ' '.join([str(param) for param in camera.params])
        f.write(f'{camera_id} {camera_model_name} {camera.width} {camera.height} {param_list}\n')

    # File format:
    # <image id> <qw> <qx> <qy> <qz> <tx> <ty> <tz> <camera id> <image name>
    # <keypoint1 x> <keypoint1 y> <keypoint1 3d point id> <keypoint2 x> <keypoint2 y> <keypoint2 3d point id> ...
    with open(images_file, 'x') as f:
        for image_name, prior_pose in zip(image_names, prior_poses):
            image_id = image_ids[image_name]
            orientation_str = ' '.join([str(x) for x in prior_pose.orientation])
            position_str = ' '.join([str(x) for x in prior_pose.position])

            f.write(f'{image_id} {orientation_str} {position_str} {camera_id} {image_name}\n')
            # Every second line should be blank (keypoints should only be
            # registered in database in our use-case, see
            # https://colmap.github.io/faq.html#reconstruct-sparse-dense-model-from-known-camera-poses)
            f.write('\n')

    with open(points3D_file, 'x') as f:
        # File should be empty
        pass


def run_shell_command(args, print_stdout=False):
    executable_name = args[0] + ' ' + args[1]

    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    if print_stdout:
        print(f'#### {executable_name} output start ####')

    while True:
        retcode = process.poll()
        line = process.stdout.readline().decode('utf-8')
        if print_stdout:
            print(line, end='')
        if retcode is not None:
            break

    if print_stdout:
        print(f'#### {executable_name} output end ####')


def run_matches_importer(colmap_executable_path, colmap_match_file_path, colmap_database_path):
    args = [
        colmap_executable_path,
        'matches_importer',
        '--database_path', colmap_database_path,
        '--match_list_path', colmap_match_file_path,
        '--match_type', 'inliers'
    ]

    run_shell_command(args, print_stdout=False)


def run_point_triangulator(colmap_executable_path, colmap_database_path, image_dir_path, colmap_sparse_input_path, colmap_sparse_triangulated_path):
    # The parameters given to the point_triangulator are *very* relaxed,
    # because at this point we assume that all the feature detections are
    # consistent (i.e. we consider them as "truth").
    args = [
        colmap_executable_path,
        'point_triangulator',
        '--database_path', colmap_database_path,
        '--image_path', image_dir_path,
        '--input_path', colmap_sparse_input_path,
        '--output_path', colmap_sparse_triangulated_path,
        '--Mapper.min_num_matches', '1',
        '--Mapper.init_image_id1', '1',
        '--Mapper.init_image_id2', '2',
        '--Mapper.tri_ignore_two_view_tracks', '0',
        '--Mapper.tri_min_angle', '0.0001',
        '--Mapper.tri_create_max_angle_error', '360',
        '--Mapper.ba_global_max_refinements', '5',
        '--Mapper.filter_max_reproj_error', '100',
        '--Mapper.filter_min_tri_angle', '0.001',
        '--Mapper.ba_refine_focal_length', '0',
        '--Mapper.ba_refine_principal_point', '0',
        '--Mapper.ba_refine_extra_params', '0',
        '--Mapper.tri_max_transitivity', '1', # Not sure what this one does, but I think it should stay at 1
        '--Mapper.tri_continue_max_angle_error', '360'
    ]

    run_shell_command(args, print_stdout=False)


def run_model_converter(colmap_executable_path, colmap_sparse_triangulated_path, colmap_sparse_plaintext_path):
    args = [
        colmap_executable_path,
        'model_converter',
        '--input_path', colmap_sparse_triangulated_path,
        '--output_path', colmap_sparse_plaintext_path,
        '--output_type', 'TXT'
    ]

    run_shell_command(args, print_stdout=False)


def run_gui(colmap_executable_path, image_dir_path, colmap_database_path, colmap_sparse_plaintext_path):
    args = [
        colmap_executable_path,
        'gui',
        '--database_path', colmap_database_path,
        '--import_path', colmap_sparse_plaintext_path,
        '--image_path', image_dir_path,
        '--Render.min_track_len', '1',
        '--Render.max_error', '1000',
        '--Render.image_connections', '1'
    ]

    subprocess.run(args)


def parse_points3d_file(colmap_sparse_plaintext_3dpoints_path):
    result = []

    # File format:
    # <point3d id> <x> <y> <z> <r> <g> <b> <error> <image id 1> <point2d idx 1> <image id 2> <point2d idx 2> ...
    # ...
    with open(colmap_sparse_plaintext_3dpoints_path, 'r') as f:
        while True:
            line = f.readline()

            if line == '':
                break

            line_trimmed = line.strip()

            if line_trimmed.startswith('#') or line_trimmed == '':
                continue

            splits = line_trimmed.split(' ', 8)

            point3d_id = int(splits[0])
            x = float(splits[1])
            y = float(splits[2])
            z = float(splits[3])
            r = int(splits[4])
            g = int(splits[5])
            b = int(splits[6])
            error = float(splits[7])

            point2d_list_str = splits[8]
            point2d_list_splits = point2d_list_str.split(' ')
            assert(len(point2d_list_splits) % 2 == 0)

            point2d_list = []
            for i in range(0, len(point2d_list_splits), 2):
                image_id = int(point2d_list_splits[i])
                point2d_idx = int(point2d_list_splits[i + 1])
                point2d_list.append((image_id, point2d_idx))

            point3d = Point3D(point_id=point3d_id, x=x, y=y, z=z, r=r, g=g, b=b, error=error, point2d_list=point2d_list)
            result.append(point3d)

    return result


def generate_landmark_list(colmap_sparse_plaintext_3dpoints_path, images_id_to_name, detections):
    # TODO Add information to each landmark:
    # - (would be nice) orientation information, maybe only 2D
    # - (maybe necessary) merge features of same type that are too close together. This will be necessary if the same traffic sign is detected twice other the course of the mapping route. Maybe this can be done with COLMAP, I saw some code with the words "merging" in it after 3D point calculation. Look for parameters that need to be tweaked (like merging criteria).

    result = []

    point3d_list = parse_points3d_file(colmap_sparse_plaintext_3dpoints_path)

    for point3d in point3d_list:
        detection_types = []
        for point2d in point3d.point2d_list:
            image_id = point2d[0]
            point2d_idx = point2d[1]

            image_name = images_id_to_name[image_id]
            point2d_detection = detections[image_name][point2d_idx]
            detection_types.append(point2d_detection.sign_type)

        assert(len(detection_types) > 0)
        # All detections that a 3D point was calculated from should have
        # the same type if our matcher did its job right (since the matcher
        # should only match points of the same type).
        assert(all([d == detection_types[0] for d in detection_types]))

        point3d_type = detection_types[0]
        confidence_score = 1 / point3d.error
        map_entry = MapLandmark(x=point3d.x, y=point3d.y, z=point3d.z, sign_type=point3d_type, confidence_score=confidence_score)
        result.append(map_entry)

    return result



# This function automates the follwing manual steps:
# 1. Delete database
# 2. Fill DB tables 'cameras', 'images', 'keypoints' `python3 fill-database.py --database_path database.db`
# 3. Import matches into DB (fills 'matches' and two_view_geometries' tables) `colmap matches_importer --database_path database.db --match_list_path matches.txt --match_type inliers`
# 4. Calculate 3D points (reads from the incomplete, manual sparse model) `colmap point_triangulator --database_path database.db --image_path images/ --input_path sparse/manual/ --output_path sparse/triangulated --Mapper.min_num_matches 1 --Mapper.init_image_id1 1 --Mapper.init_image_id2 2 --Mapper.tri_ignore_two_view_tracks 0 --Mapper.tri_min_angle 0.0001 --Mapper.tri_create_max_angle_error 360 --Mapper.ba_global_max_refinements 5 --Mapper.filter_max_reproj_error 100 --Mapper.filter_min_tri_angle 0.001 --Mapper.ba_refine_focal_length 0 --Mapper.ba_refine_principal_point 0 --Mapper.ba_refine_extra_params 0 --Mapper.tri_max_transitivity 5 --Mapper.tri_continue_max_angle_error 360`
# 5. Convert to plain-text files `colmap model_converter --input_path sparse/triangulated/ --output_path sparse/triangulated-txt --output_type TXT`
# 6. Show model with `colmap gui --database_path database.db --import_path sparse/triangulated --image_path images --Render.min_track_len 1 --Render.max_error 20 --Render.image_connections 1`
def triangulate(colmap_executable_path, image_dir_path, detections, matches, gt_estimator, colmap_working_dir_path):
    colmap_database_path = os.path.join(colmap_working_dir_path, 'datbase.db')
    colmap_match_file_path = os.path.join(colmap_working_dir_path, 'matches.txt')
    colmap_sparse_input_path = os.path.join(colmap_working_dir_path, 'sparse/in')
    colmap_sparse_triangulated_path = os.path.join(colmap_working_dir_path, 'sparse/triangulated')
    colmap_sparse_plaintext_path = os.path.join(colmap_working_dir_path, 'sparse/plaintext')
    colmap_sparse_plaintext_3dpoints_path = os.path.join(colmap_sparse_plaintext_path, 'points3D.txt')


    # Prepare directory structure
    if os.path.exists(colmap_working_dir_path):
        shutil.rmtree(colmap_working_dir_path)

    os.makedirs(colmap_working_dir_path)
    os.makedirs(colmap_sparse_input_path)
    os.makedirs(colmap_sparse_triangulated_path)
    os.makedirs(colmap_sparse_plaintext_path)


    # Prepare COLMAP database and input files
    image_names = images.get_image_names(image_dir_path)
    timestamps = get_timestamps_from_images(image_names)

    camera = get_camera_malaga_extract_07_right()
    prior_poses = get_poses(gt_estimator, timestamps)

    camera_id, images_name_to_id = fill_database(colmap_database_path, camera, image_names, prior_poses, detections)

    write_matches_file(colmap_match_file_path, image_names, matches)
    fill_sparse_in_dir(colmap_sparse_input_path, camera, image_names, prior_poses, camera_id, images_name_to_id)


    # Run COLMAP commands
    print('Importing matches...')
    run_matches_importer(colmap_executable_path, colmap_match_file_path, colmap_database_path)

    print('Triangulating points...')
    run_point_triangulator(colmap_executable_path, colmap_database_path, image_dir_path, colmap_sparse_input_path, colmap_sparse_triangulated_path)

    print('Converting model to plain-text...')
    run_model_converter(colmap_executable_path, colmap_sparse_triangulated_path, colmap_sparse_plaintext_path)

    print('Opening model viewer...')
    run_gui(colmap_executable_path, image_dir_path, colmap_database_path, colmap_sparse_plaintext_path)


    # Find out which 3D point is which type of feature
    print('Constructing landmark list...')
    images_id_to_name = {v: k for k, v in images_name_to_id.items()}
    landmark_list = generate_landmark_list(colmap_sparse_plaintext_3dpoints_path, images_id_to_name, detections)

    return landmark_list