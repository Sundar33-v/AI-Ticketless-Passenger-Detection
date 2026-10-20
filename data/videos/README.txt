Place recorded bus videos in this folder (video files here are gitignored).
Example (config/config.yaml):
    camera:
      source_type: "video"
      video:
        path: "data/videos/bus_video.mp4"
Or choose a file for a single run without editing the config:
    scripts\run_detection.bat --source video --path data\videos\my_other_video.mp4
    scripts\run_detection.bat --source video --choose-file
Allowed formats: .mp4, .avi, .mov, .mkv (camera.video.allowed_extensions).
Videos are not converted. Recorded video goes through the same YOLO person
detection, ByteTrack tracking, entry-line, ticket and database pipeline as live cameras.
