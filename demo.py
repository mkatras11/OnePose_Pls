import hydra
from omegaconf.dictconfig import DictConfig
from tqdm import tqdm
from loguru import logger
import os
os.environ["TORCH_USE_RTLD_GLOBAL"] = "TRUE"  # important for DeepLM module, this line should before import torch
import os.path as osp
import glob
import numpy as np
import natsort
import torch
import matplotlib.pyplot as plt

from src.utils import data_utils
from src.utils import vis_utils
from src.utils.metric_utils import ransac_PnP
from src.utils.metric_utils import query_pose_error 
from src.datasets.OnePosePlus_inference_dataset import OnePosePlusInferenceDataset
from src.inference.inference_OnePosePlus import build_model
from src.local_feature_object_detector.local_feature_2D_detector import LocalFeatureObjectDetector

from time import time

def get_default_paths(cfg, data_root, data_dir, sfm_model_dir):
    sfm_ws_dir = osp.join(
        sfm_model_dir,
        "sfm_ws",
        "model",
    )

    img_lists = []
    color_dir = osp.join(data_dir, "color")
    img_lists += glob.glob(color_dir + "/*.png", recursive=True)
    
    img_lists = natsort.natsorted(img_lists)

    # Visualize detector:
    vis_detector_dir = osp.join(data_dir, "detector_vis")
    if osp.exists(vis_detector_dir):
        os.system(f"rm -rf {vis_detector_dir}")
    os.makedirs(vis_detector_dir, exist_ok=True)
    det_box_vis_video_path = osp.join(data_dir, "det_box.mp4")

    # Visualize pose:
    vis_box_dir = osp.join(data_dir, "pred_vis")
    if osp.exists(vis_box_dir):
        os.system(f"rm -rf {vis_box_dir}")
    os.makedirs(vis_box_dir, exist_ok=True)
    demo_video_path = osp.join(data_dir, "demo_video.mp4")

    # intrin_full_dir = osp.join(data_dir, "origin_intrin")
    intrin_full_path = osp.join(data_dir, "intrinsics.txt")
    intrin_full_dir = osp.join(data_dir, 'intrin_full')

    bbox3d_path = osp.join(data_root, 'box3d_corners.txt')
    paths = {
        "data_root": data_root,
        "data_dir": data_dir,
        "sfm_dir": sfm_model_dir,
        "sfm_ws_dir": sfm_ws_dir,
        "bbox3d_path": bbox3d_path,
        "intrin_full_path": intrin_full_path,
        "intrin_full_dir": intrin_full_dir,
        "vis_detector_dir": vis_detector_dir,
        "vis_box_dir": vis_box_dir,
        "det_box_vis_video_path": det_box_vis_video_path,
        "demo_video_path": demo_video_path,
    }
    return img_lists, paths

def inference_core(cfg, data_root, seq_dir, sfm_model_dir, i=0):
    img_list, paths = get_default_paths(cfg, data_root, seq_dir, sfm_model_dir)
    dataset = OnePosePlusInferenceDataset(
        paths['sfm_dir'],
        img_list,
        load_3d_coarse=cfg.datamodule.load_3d_coarse,
        shape3d=cfg.datamodule.shape3d_val,
        img_pad=cfg.datamodule.img_pad,
        img_resize=None,
        df=cfg.datamodule.df,
        pad=cfg.datamodule.pad3D,
        load_pose_gt=False,
        n_images=None,
        demo_mode=True,
        preload=True,
    )

    # NOTE: if you find pose estimation results are not good, problem maybe due to the poor object detection at the very beginning of the sequence.
    # You can set `output_results=True`, the detection results will thus be saved in the `detector_vis` directory in folder of the test sequence.
    local_feature_obj_detector = LocalFeatureObjectDetector(
        sfm_ws_dir=paths["sfm_ws_dir"],
        output_results=True, 
        detect_save_dir=paths["vis_detector_dir"],
    )
    match_2D_3D_model = build_model(cfg['model']["OnePosePlus"], cfg['model']['pretrained_ckpt'])
    match_2D_3D_model.cuda()

    K, _ = data_utils.get_K(paths["intrin_full_path"])

    bbox3d = np.loadtxt(paths["bbox3d_path"])
    pred_poses = {}  # {id:[pred_pose, inliers]}
    eval_poses = []
    for id in tqdm(range(len(dataset))):
        data = dataset[id]
        query_image = data['query_image']
        query_image_path = data['query_image_path']
        
        K_crop  = np.loadtxt(query_image_path.replace("color", "intrin_ba").replace("png", "txt"))
        pose_gt = np.loadtxt(query_image_path.replace("color", "poses_ba").replace("png", "txt"))
        
        # Detect object:
        #if id == 0:
            # Detect object by 2D local feature matching for the first frame:
        #    bbox, inp_crop, K_crop = local_feature_obj_detector.detect(query_image, query_image_path, K)
        #else:
            # Use 3D bbox and previous frame's pose to yield current frame 2D bbox:
        #    previous_frame_pose, inliers = pred_poses[id - 1]

        #    if len(inliers) < 20:
                # Consider previous pose estimation failed, reuse local feature object detector:
        #        bbox, inp_crop, K_crop = local_feature_obj_detector.detect(
        #            query_image, query_image_path, K
        #        )
        #    else:
        #        (
        #            bbox,
        #            inp_crop,
        #            K_crop,
        #        ) = local_feature_obj_detector.previous_pose_detect(
        #            query_image_path, K, previous_frame_pose, bbox3d
        #        )
        #data.update({"query_image": inp_crop.cuda()})
        
        data.update({"query_image": query_image.cuda()})

        # Perform keypoint-free 2D-3D matching and then estimate object pose of query image by PnP:
        t1 = time()
        with torch.no_grad():
            match_2D_3D_model(data)
        mkpts_3d = data["mkpts_3d_db"].cpu().numpy() # N*3
        mkpts_query = data["mkpts_query_f"].cpu().numpy() # N*2
        pose_pred, _, inliers, _ = ransac_PnP(K_crop, mkpts_query, mkpts_3d, scale=1000, pnp_reprojection_error=7, img_hw=[512,512], use_pycolmap_ransac=True)
        print(time() - t1)

        pred_poses[id] = [pose_pred, inliers]
        
        eval_poses.append(query_pose_error(pose_pred, pose_gt, unit='cm'))
        poses = [pose_gt, pose_pred]

        # # Evaluate 2D-3D matching results:
        # from matplotlib.patches import ConnectionPatch
        # print(mkpts_3d.shape, mkpts_query.shape, inliers.shape)            
        # if eval_poses[i][0] > 80 and eval_poses[i][0] < 100:
        #     fig = plt.figure(figsize=(10,5))

        #     ax2 = fig.add_subplot(121)
        #     ax1 = fig.add_subplot(122)
        #     ax1.imshow(query_image[0, 0].cpu().numpy())
        #     ax1.scatter(mkpts_query[:, 0], mkpts_query[:, 1], c='r', s=3)
        #     ax2.scatter(mkpts_3d[:, 0], mkpts_3d[:, 1], c='b', s=3)
    
        #     ax1.axis('off')
        #     ax2.axis('off')
        #     ax1.axis('equal')
        #     ax2.axis('equal')

        #     ax1.set_title('Query image matched keypoints')
        #     ax2.set_title('Point cloud matched keypoints')

        #     for j in range(len(inliers)):
        #         # use ConnectionPatch to draw lines between points in different axes
        #         con = ConnectionPatch(xyA=(mkpts_query[inliers[j], 0], mkpts_query[inliers[j], 1]), xyB=(mkpts_3d[inliers[j], 0], mkpts_3d[inliers[j], 1]), coordsA="data", coordsB="data", axesA=ax1, axesB=ax2, color="green", linewidth=0.5)
        #         ax1.add_artist(con)
        #     plt.show()
        
        # i+=1

        # Visualize:
        vis_utils.save_demo_image(
            poses,
            K, #K_crop
            image_path= query_image_path.replace('color', 'color_full'),
            box3d=bbox3d,
            draw_box=len(inliers) > 20,
            save_path=osp.join(paths["vis_box_dir"], f"{id}.jpg"),
        )
        
    eval_poses = np.array(eval_poses)
    print("Rotation and translation error:", np.nanmean(eval_poses, axis=0))

    # Calculate the 5cm-5deg, 3cm-3deg and 1cm-1deg metric but count 180deg as correct:
    cm_deg_list = []
    for cm, deg in [(5, 5), (3, 3), (1, 1)]:
        cm_deg_list.append(
            np.mean(
                np.logical_and(
                    eval_poses[:, 0] < deg,
                    eval_poses[:, 1] * 100 < cm,
                ).astype(np.float)
            )
        )
    print("5cm-5deg, 3cm-3deg and 1cm-1deg metric:", cm_deg_list)

    # Create the pose error plots
    plt.figure(figsize=(10, 5))
    # plt.plot(eval_poses[:, 0], 'b.' , label="rotation error (deg)")
    # plt.plot(eval_poses[:, 1]*100, 'r.', label="translation error (cm)")
    plt.plot(eval_poses[:, 0], label="rotation error (deg)")
    plt.plot(eval_poses[:, 1]*100, label="translation error (cm)")
    plt.legend()
    plt.xlabel("Frame")
    plt.ylabel("Error")
    plt.title("Pose estimation error")
    #save plot as pdf
    plt.savefig(osp.join(paths["data_dir"], f"pose_error.pdf"), format='pdf')



    
    # Output video to visualize estimated poses:
    logger.info(f"Generate demo video begin...")
    vis_utils.make_video(paths["vis_box_dir"], paths["demo_video_path"])

def inference(cfg):
    data_dirs = cfg.data_base_dir
    sfm_model_dirs = cfg.sfm_base_dir

    if isinstance(data_dirs, str) and isinstance(sfm_model_dirs, str):
        data_dirs = [data_dirs]
        sfm_model_dirs = [sfm_model_dirs]

    for data_dir, sfm_model_dir in tqdm(
        zip(data_dirs, sfm_model_dirs), total=len(data_dirs)
    ):
        splits = data_dir.split(" ")
        data_root = splits[0]
        for seq_name in splits[1:]:
            seq_dir = osp.join(data_root, seq_name)
            logger.info(f"Eval {seq_dir}")
            inference_core(cfg, data_root, seq_dir, sfm_model_dir)

@hydra.main(config_path="configs/", config_name="config.yaml")
def main(cfg: DictConfig):
    globals()[cfg.type](cfg)


if __name__ == "__main__":
    main()
