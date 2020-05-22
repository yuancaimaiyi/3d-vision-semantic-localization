from sklearn.metrics import mean_squared_error as mse
import re
import numpy as np
import random as rand
import localization
from triangulation import MapLandmark, ImagePose, get_camera_malaga_extract_07_right, ColmapCamera, malaga_car_pose_to_camera_pose
import detection
import util
import cv2
import sys
import pickle
import ground_truth_estimator
from ground_truth_estimator import GroundTruthEstimator
np.set_printoptions(threshold=sys.maxsize)


# detections
# timestamps = [1261230001.030210]
# timestamps = [1261230001.080210]
# timestamps = [1261230001.130214]
# timestamps = [1261230001.180217]
# timestamps = [1261230036.630518]
# timestamps = [1261230001.430199]

DETECTIONS_PATH = "/home/patricia/3D/detections/detections_07_right.pickle"

gps_full_data = np.load("./transform_routes/transf_routes_overlap/routeFull_in_route7coords.npy")
imu_full_data = np.genfromtxt("/home/patricia/3D/malaga-urban-dataset_IMU.txt", skip_header=1)
def get_rank(timestamps):
    SCORES_PATH = "/home/patricia/3D/queryscores/07_right_map/img_CAMERA1_%s_right.jpg.pickle"%(str(timestamps))
    POSES_PATH = "/home/patricia/Downloads/map_07_possible_poses.pickle"
#    gps_full_data, imu_full_data = ground_truth_estimator.load_gps_and_imu_data('./data/07/gps.csv', './data/07/imu.csv')
#    gps_full_data = np.load("./transform_routes/transf_routes_overlap/routeFull_in_route7coords.npy")
#    imu_full_data = np.genfromtxt("/home/patricia/3D/malaga-urban-dataset_IMU.txt", skip_header=1)
    estimator = GroundTruthEstimator(gps_full_data, imu_full_data, print_kf_progress=True)
    positions = estimator.get_position(timestamps, method='cubic')
#    print("estimated gps:")
#    print(positions)
#    gps = positions[0][0:2]
    gps = positions[0:2]
    try:
        with open(DETECTIONS_PATH, 'rb') as file:
            detections = pickle.load(file)
        with open(SCORES_PATH, 'rb') as file:
            pickle_scores = pickle.load(file)
        with open('/home/patricia/Downloads/map_07_possible_poses.pickle', 'rb') as file:
            pickle_poses = pickle.load(file)
    except FileNotFoundError:
        print("---file not found---")
        return 
    else:
#        num_detections = np.asarray(detections.get('img_CAMERA1_%s_right.jpg'%(str(timestamps[0])))).shape[0]
        num_detections = np.asarray(detections.get('img_CAMERA1_%s_right.jpg'%(str(timestamps)))).shape[0]
        print("detected signs:")
        print(num_detections)
        num_scores = 1
        scores = []
        poses = []
        
        for dim in pickle_scores.shape:
            num_scores *= dim # a*b*c from dimension (a,b,c)
        for i in range(num_scores):
            pose_idx = np.unravel_index(i, pickle_scores.shape)
            scores.append(pickle_scores[pose_idx])
            poses.append(pickle_poses[pose_idx])
        scores = np.asarray([scores])
        poses = np.asarray(poses)
#        print("dimensions of scores and x,y poses")
#        print(scores.shape)
#        print(poses[:,0:2].shape)
        
        combo = np.append(poses[:,0:2],scores.T, axis=1)
        sorted_combo_idx = np.argsort(combo[:,-1])
        sorted_combo = combo[sorted_combo_idx]
        sorted_predicts = np.delete(sorted_combo, np.s_[-1:], axis=1)
        # leave only (x,y)
        top_predicts = sorted_predicts[-100:]
        # print(top_predicts)
        
        correct = 0 
        errors = []
        for predict in top_predicts:
            errors.append(mse(gps,predict))
            # print(predict,mse(gps,predict))
        
        # percentage of correctness in each query (by Pedro)
        precision = []
        for i in range(len(errors)):
            if errors[-i]<5:
                rank = i
                precision.append([0 for j in range(i-1)]+[1 for j in range(100-i+1)])
                break
        if not precision:
            rank = 100
            precision.append([0 for j in range(100)])
        
        # print(precision)
        # print("precision and rank")
        # print(rank)
        # print(len(precision[0]),rank)
        correctness = np.sum(precision)
        precision = np.asarray(precision)
        print("wat we call rank...")
        print(correctness)
    return precision

def iterate_queries():
#    timestamps = [1261230001.180217]
#    timestamps = [1261229981.580023]
#    rank = get_rank(timestamps)
    with open(DETECTIONS_PATH, 'rb') as file:
        detections = pickle.load(file)
    rank = []
    for key in detections.keys():
        pattern = re.compile('img_CAMERA1_(\d*.\d*)_(right|left).jpg')
        match = pattern.match(key)
        timestamp = match.group(1)
        print("---timestamp---"+timestamp) 
        if get_rank(timestamp) is not None:
            rank.append(get_rank(timestamp)[0])
    return rank

if __name__ == '__main__':
    # timestamps = [1261230001.430199]
    # timestamps = [1261230001.530205]
    # rank = get_rank(timestamps)
    rank = iterate_queries()
    print("final rank")
    print(rank)
    rank = np.asarray(rank)
    print(rank.shape)
