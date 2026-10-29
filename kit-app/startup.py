import carb

USD = "D:/Piyush/Blender_usd/Blender_usd/new_update/updated_timelapse.usd"

try:
    import omni.usd
    result = omni.usd.get_context().open_stage(USD)
    carb.log_info(f"[PlanLift startup] open_stage -> {result}")
except Exception as e:
    carb.log_error(f"[PlanLift startup] {e}")
