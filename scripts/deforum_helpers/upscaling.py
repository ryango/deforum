import os
import numpy as np
import cv2
from pathlib import Path
from tqdm import tqdm
from PIL import Image
from modules.scripts_postprocessing import PostprocessedImage
from modules import devices
import shutil
from queue import Queue, Empty
import modules.scripts as scr
from .frame_interpolation import clean_folder_name
from rife.inference_video import duplicate_pngs_from_folder
from .video_audio_utilities import get_quick_vid_info, vid2frames, ffmpeg_stitch_video

def process_upscale_vid_upload_logic(file, selected_tab, upscaling_resize, upscaling_resize_w, upscaling_resize_h, upscaling_crop, extras_upscaler_1, extras_upscaler_2, extras_upscaler_2_visibility, vid_file_name, keep_imgs, f_location, f_crf, f_preset):
    print("got a request to *upscale* an existing video.")

    in_vid_fps, _, _ = get_quick_vid_info(file.name)
    folder_name = clean_folder_name(Path(vid_file_name).stem)
    outdir_no_tmp = os.path.join(os.getcwd(), 'outputs', 'frame-upscaling', folder_name)
    i = 1
    while os.path.exists(outdir_no_tmp):
        outdir_no_tmp = os.path.join(os.getcwd(), 'outputs', 'frame-upscaling', folder_name + '_' + str(i))
        i += 1

    outdir = os.path.join(outdir_no_tmp, 'tmp_input_frames')
    os.makedirs(outdir, exist_ok=True)
    
    vid2frames(video_path=file.name, video_in_frame_path=outdir, overwrite=True, extract_from_frame=0, extract_to_frame=-1, numeric_files_output=True, out_img_format='png')
    
    process_video_upscaling(selected_tab, upscaling_resize, upscaling_resize_w, upscaling_resize_h, upscaling_crop, extras_upscaler_1, extras_upscaler_2, extras_upscaler_2_visibility, orig_vid_fps=in_vid_fps, real_audio_track=file.name, raw_output_imgs_path=outdir, img_batch_id=None, ffmpeg_location=f_location, ffmpeg_crf=f_crf, ffmpeg_preset=f_preset, keep_upscale_imgs=keep_imgs, orig_vid_name=folder_name)

def process_video_upscaling(resize_mode, upscaling_resize, upscaling_resize_w, upscaling_resize_h, upscaling_crop, extras_upscaler_1, extras_upscaler_2, extras_upscaler_2_visibility, orig_vid_fps, real_audio_track, raw_output_imgs_path, img_batch_id, ffmpeg_location, ffmpeg_crf, ffmpeg_preset, keep_upscale_imgs, orig_vid_name):
    devices.torch_gc()

    print("Upscaling progress (it's OK if it finishes before 100%):")

    upscaled_path = os.path.join(raw_output_imgs_path, 'upscaled_frames')
    if orig_vid_name is not None: # upscaling a video (deforum or unrelated)
        custom_upscale_path = "{}_{}".format(upscaled_path, orig_vid_name)
    else: # upscaling after a deforum run:
        custom_upscale_path = "{}_{}".format(upscaled_path, img_batch_id)
    
    temp_convert_raw_png_path = os.path.join(raw_output_imgs_path, "tmp_upscale_folder")
    duplicate_pngs_from_folder(raw_output_imgs_path, temp_convert_raw_png_path, img_batch_id, orig_vid_name)

    videogen = []
    for f in os.listdir(temp_convert_raw_png_path):
        # double check for old _depth_ files, not really needed probably but keeping it for now
        if '_depth_' not in f:
            videogen.append(f)
            
    videogen.sort(key= lambda x:int(x.split('.')[0]))
    vid_out = None

    if not os.path.exists(custom_upscale_path):
        os.mkdir(custom_upscale_path)

    # Upscaling is a slow and demanding operation, so we don't need as much parallelization here
    for i in tqdm(range(len(videogen)), desc="Upscaling"):
        lastframe = videogen[i]
        img_path = os.path.join(temp_convert_raw_png_path, lastframe)
        image = process_frame(resize_mode, Image.open(img_path).convert("RGB"), upscaling_resize, upscaling_resize_w, upscaling_resize_h, upscaling_crop, extras_upscaler_1, extras_upscaler_2, extras_upscaler_2_visibility)
        filename = '{}/{:0>7d}.png'.format(custom_upscale_path, i)
        image.save(filename)

    shutil.rmtree(temp_convert_raw_png_path)
    # stitch video from upscaled frames, and add audio if needed
    try:
        print (f"*Passing upsc frames to ffmpeg...*")
        vid_out_path = stitch_video(img_batch_id, orig_vid_fps, custom_upscale_path, real_audio_track, ffmpeg_location, resize_mode, upscaling_resize, upscaling_resize_w, upscaling_resize_h, extras_upscaler_1, extras_upscaler_2, extras_upscaler_2_visibility, ffmpeg_crf, ffmpeg_preset, keep_upscale_imgs, orig_vid_name)
        # remove folder with raw (non-upscaled) vid input frames in case of input VID and not PNGs
        if orig_vid_name is not None:
            shutil.rmtree(raw_output_imgs_path)
    except Exception as e:
        print(f'Video stitching gone wrong. *Upscaled frames were saved to HD as backup!*. Actual error: {e}')

    devices.torch_gc()

def process_frame(resize_mode, image, upscaling_resize, upscaling_resize_w, upscaling_resize_h, upscaling_crop, extras_upscaler_1, extras_upscaler_2, extras_upscaler_2_visibility):
    pp = PostprocessedImage(image)
    postproc = scr.scripts_postproc
    upscaler_script = next(s for s in postproc.scripts if s.name == "Upscale")
    upscaler_script.process(pp, resize_mode, upscaling_resize, upscaling_resize_w, upscaling_resize_h, upscaling_crop, extras_upscaler_1, extras_upscaler_2, extras_upscaler_2_visibility)
    return pp.image

def stitch_video(img_batch_id, fps, img_folder_path, audio_path, ffmpeg_location, resize_mode, upscaling_resize, upscaling_resize_w, upscaling_resize_h, extras_upscaler_1, extras_upscaler_2, extras_upscaler_2_visibility, f_crf, f_preset, keep_imgs, orig_vid_name):        
    parent_folder = os.path.dirname(img_folder_path)
    grandparent_folder = os.path.dirname(parent_folder)
    if orig_vid_name is not None:
        mp4_path = os.path.join(grandparent_folder, str(orig_vid_name) +'_upscaled_' + (('by_' + str(upscaling_resize).replace('.', '-')) if resize_mode == 0 else f"to_{upscaling_resize_w}_{upscaling_resize_h}")) + f"_with_{extras_upscaler_1}" + (f"_then_{extras_upscaler_2}" if extras_upscaler_2_visibility > 0 else "")
    else:
        mp4_path = os.path.join(parent_folder, str(img_batch_id) +'_upscaled_' + (('by_' + str(upscaling_resize).replace('.', '-')) if resize_mode == 0 else f"to_{upscaling_resize_w}_{upscaling_resize_h}")) + f"_with_{extras_upscaler_1}_then_{extras_upscaler_2}"
    
    mp4_path = mp4_path + '.mp4'

    t = os.path.join(img_folder_path, "%07d.png")
    add_soundtrack = 'None'
    if not audio_path is None:
        add_soundtrack = 'File'
        
    exception_raised = False
    try:
        ffmpeg_stitch_video(ffmpeg_location=ffmpeg_location, fps=fps, outmp4_path=mp4_path, stitch_from_frame=0, stitch_to_frame=1000000, imgs_path=t, add_soundtrack=add_soundtrack, audio_path=audio_path, crf=f_crf, preset=f_preset)
    except Exception as e:
        exception_raised = True
        print(f"An error occurred while stitching the video: {e}")

    if not exception_raised and not keep_imgs:
        shutil.rmtree(img_folder_path)

    if (keep_imgs and orig_vid_name is not None) or (orig_vid_name is not None and exception_raised is True):
        shutil.move(img_folder_path, grandparent_folder)

    return mp4_path
