"""KIE Image Demo.

Combines Text Detection + Text Recognition + KIE stages. Configs can be
provided for each of the stages. The output is an image containing bounding
boxes around text with KIE annotations and a json file containing these
annotations.
"""

from argparse import ArgumentParser

import mmcv
import torch
from mmcv.image.misc import tensor2imgs
from mmdet.apis import init_detector

from mmocr.apis.inference import model_inference
from mmocr.datasets.kie_dataset import KIEDataset
from mmocr.datasets.pipelines.crop import crop_img
from mmocr.utils.fileio import list_from_file


def generate_kie_labels(result, boxes, class_list):
    idx_to_cls = {}
    if class_list is not None:
        for line in list_from_file(class_list):
            class_idx, class_label = line.strip().split()
            idx_to_cls[class_idx] = class_label

    max_value, max_idx = torch.max(result['nodes'].detach().cpu(), -1)
    node_pred_label = max_idx.numpy().tolist()
    node_pred_score = max_value.numpy().tolist()
    labels = []
    for i in range(len(boxes)):
        pred_label = str(node_pred_label[i])
        if pred_label in idx_to_cls:
            pred_label = idx_to_cls[pred_label]
        pred_score = node_pred_score[i]
        labels.append((pred_label, pred_score))
    return labels


def visualize_kie_output(model, data, result, out_file=None, show=False):
    """Visualizes KIE output."""
    img_tensor = data['img'].data
    img_meta = data['img_metas'].data
    gt_bboxes = data['gt_bboxes'].data.numpy().tolist()
    img = tensor2imgs(img_tensor.unsqueeze(0), **img_meta['img_norm_cfg'])[0]
    h, w, _ = img_meta['img_shape']
    img_show = img[:h, :w, :]
    model.show_result(
        img_show, result, gt_bboxes, show=show, out_file=out_file)


def det_recog_kie_inference(args, det_model, recog_model, kie_model):
    image_path = args.img
    end2end_res = {'filename': image_path}
    end2end_res['result'] = []

    image = mmcv.imread(image_path)
    # detection
    det_result = model_inference(det_model, image)
    bboxes = det_result['boundary_result']

    # recognition
    box_imgs = []
    for bbox in bboxes:
        box_res = {}
        box_res['box'] = [round(x) for x in bbox[:-1]]
        box_res['box_score'] = float(bbox[-1])
        box = bbox[:8]
        if len(bbox) > 9:
            min_x = min(bbox[0:-1:2])
            min_y = min(bbox[1:-1:2])
            max_x = max(bbox[0:-1:2])
            max_y = max(bbox[1:-1:2])
            box = [min_x, min_y, max_x, min_y, max_x, max_y, min_x, max_y]
        box_img = crop_img(image, box)
        if args.batch_mode:
            box_imgs.append(box_img)
        else:
            recog_result = model_inference(recog_model, box_img)
            text = recog_result['text']
            text_score = recog_result['score']
            if isinstance(text_score, list):
                text_score = sum(text_score) / max(1, len(text))
            box_res['text'] = text
            box_res['text_score'] = text_score
        end2end_res['result'].append(box_res)

    if args.batch_mode:
        batch_size = args.batch_size
        for chunk_idx in range(len(box_imgs) // batch_size + 1):
            start_idx = chunk_idx * batch_size
            end_idx = (chunk_idx + 1) * batch_size
            chunk_box_imgs = box_imgs[start_idx:end_idx]
            if len(chunk_box_imgs) == 0:
                continue
            recog_results = model_inference(
                recog_model, chunk_box_imgs, batch_mode=True)
            for i, recog_result in enumerate(recog_results):
                text = recog_result['text']
                text_score = recog_result['score']
                if isinstance(text_score, list):
                    text_score = sum(text_score) / max(1, len(text))
                end2end_res['result'][start_idx + i]['text'] = text
                end2end_res['result'][start_idx + i]['text_score'] = text_score

    # KIE
    annotations = end2end_res['result']
    kie_dataset = KIEDataset(dict_file=kie_model.cfg.data.test.dict_file)
    ann_info = kie_dataset._parse_anno_info(annotations)
    kie_result, data = model_inference(
        kie_model, image, ann=ann_info, return_data=True)
    # visualize KIE results
    visualize_kie_output(
        kie_model, data, kie_result, out_file=args.out_file, show=args.imshow)
    gt_bboxes = data['gt_bboxes'].data.numpy().tolist()
    labels = generate_kie_labels(kie_result, gt_bboxes, kie_model.class_list)
    for i in range(len(gt_bboxes)):
        end2end_res['result'][i]['label'] = labels[i][0]
        end2end_res['result'][i]['label_score'] = labels[i][1]

    return end2end_res


def main():
    parser = ArgumentParser()
    parser.add_argument('img', type=str, help='Input Image file.')
    parser.add_argument(
        'out_file', type=str, help='Output file name of the visualized image.')
    parser.add_argument(
        '--det-config',
        type=str,
        default='./configs/textdet/psenet/'
        'psenet_r50_fpnf_600e_icdar2015.py',
        help='Text detection config file.')
    parser.add_argument(
        '--det-ckpt',
        type=str,
        default='https://download.openmmlab.com/'
        'mmocr/textdet/psenet/'
        'psenet_r50_fpnf_600e_ctw1500_20210401-216fed50.pth',
        help='Text detection checkpint file (local or url).')
    parser.add_argument(
        '--recog-config',
        type=str,
        default='./configs/textrecog/sar/'
        'sar_r31_parallel_decoder_academic.py',
        help='Text recognition config file.')
    parser.add_argument(
        '--recog-ckpt',
        type=str,
        default='https://download.openmmlab.com/'
        'mmocr/textrecog/sar/'
        'sar_r31_parallel_decoder_academic-dba3a4a3.pth',
        help='Text recognition checkpint file (local or url).')
    parser.add_argument(
        '--kie-config',
        type=str,
        default='./configs/kie/sdmgr/'
        'sdmgr_unet16_60e_wildreceipt.py',
        help='Key information extraction config file.')
    parser.add_argument(
        '--kie-ckpt',
        type=str,
        default='https://download.openmmlab.com/'
        'mmocr/kie/sdmgr/'
        'sdmgr_unet16_60e_wildreceipt_20210520-7489e6de.pth',
        help='Key information extraction checkpint file (local or url).')
    parser.add_argument(
        '--batch-mode',
        action='store_true',
        help='Whether use batch mode for text recognition.')
    parser.add_argument(
        '--batch-size',
        type=int,
        default=4,
        help='Batch size for text recognition inference '
        'if batch_mode is True above.')
    parser.add_argument(
        '--device', default='cuda:0', help='Device used for inference.')
    parser.add_argument(
        '--imshow',
        action='store_true',
        help='Whether show image with OpenCV.')
    args = parser.parse_args()

    if args.device == 'cpu':
        args.device = None
    # build detect model
    detect_model = init_detector(
        args.det_config, args.det_ckpt, device=args.device)
    if hasattr(detect_model, 'module'):
        detect_model = detect_model.module
    if detect_model.cfg.data.test['type'] == 'ConcatDataset':
        detect_model.cfg.data.test.pipeline = \
            detect_model.cfg.data.test['datasets'][0].pipeline

    # build recog model
    recog_model = init_detector(
        args.recog_config, args.recog_ckpt, device=args.device)
    if hasattr(recog_model, 'module'):
        recog_model = recog_model.module
    if recog_model.cfg.data.test['type'] == 'ConcatDataset':
        recog_model.cfg.data.test.pipeline = \
            recog_model.cfg.data.test['datasets'][0].pipeline

    # build KIE model
    kie_model = init_detector(
        args.kie_config, args.kie_ckpt, device=args.device)

    result = det_recog_kie_inference(args, detect_model, recog_model,
                                     kie_model)
    print(f'result: {result}')
    mmcv.dump(result, args.out_file + '.json', ensure_ascii=False, indent=4)


if __name__ == '__main__':
    main()