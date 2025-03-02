# Armored Turtle Automated Filament Control
#
# Copyright (C) 2024 Armored Turtle
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
import re
from configfile import error
from datetime import datetime
try:
    from extras.AFC_respond import AFCprompt
except:
    raise error("Error trying to import AFC_respond, please rerun install-afc.sh script in your AFC-Klipper-Add-On directory then restart klipper")

def load_config(config):
    return afcFunction(config)

class afcFunction:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.printer.register_event_handler("afc_stepper:register_macros",self.register_lane_macros)
        self.printer.register_event_handler("afc_hub:register_macros",self.register_hub_macros)
        self.errorLog = {}
        self.pause    = False

    def register_lane_macros(self, lane_obj):
        """
        Callback function to register macros with proper lane names so that klipper errors out correctly when users supply lanes names that
        are not valid

        :param lane_obj: object for lane to register
        """
        self.AFC.gcode.register_mux_command('TEST',         "LANE", lane_obj.name, self.cmd_TEST,         desc=self.cmd_TEST_help)
        self.AFC.gcode.register_mux_command('HUB_CUT_TEST', "LANE", lane_obj.name, self.cmd_HUB_CUT_TEST, desc=self.cmd_HUB_CUT_TEST_help)

    def register_hub_macros(self, hub_obj):
        """
        Callback function to register macros with proper hub names so that klipper errors out correctly when users supply hub names that
        are not valid

        :param hub_obj: object for hub to register
        """
        self.AFC.gcode.register_mux_command('SET_BOWDEN_LENGTH', 'HUB', hub_obj.name, self.cmd_SET_BOWDEN_LENGTH, desc=self.cmd_SET_BOWDEN_LENGTH_help)

    def handle_connect(self):
        """
        Handle the connection event.
        This function is called when the printer connects. It looks up AFC info
        and assigns it to the instance variable `self.AFC`.
        """
        self.AFC = self.printer.lookup_object('AFC')
        self.logger = self.AFC.logger
        self.AFC.gcode.register_command('CALIBRATE_AFC'  , self.cmd_CALIBRATE_AFC  , desc=self.cmd_CALIBRATE_AFC_help)
        self.AFC.gcode.register_command('AFC_CALIBRATION', self.cmd_AFC_CALIBRATION, desc=self.cmd_AFC_CALIBRATION_help)
        self.AFC.gcode.register_command('ALL_CALIBRATION', self.cmd_ALL_CALIBRATION, desc=self.cmd_ALL_CALIBRATION_help)

    def ConfigRewrite(self, rawsection, rawkey, rawvalue, msg=""):
        taskdone = False
        sectionfound = False
        # Creating regex pattern based off rawsection
        pattern = re.compile("^\[\s*{}\s*\]".format(rawsection))
        for filename in os.listdir(self.AFC.cfgloc):
            file_path = os.path.join(self.AFC.cfgloc, filename)
            if os.path.isfile(file_path) and filename.endswith(".cfg"):
                with open(file_path, 'r') as f:
                    dataout = ''
                    for line in f:
                        # If previous section found and line starts with bracket, means that this line is another section
                        #  need to put sectionfound to false to not update wrong sections if rawkey is not found
                        if sectionfound and line.startswith("["): sectionfound = False

                        if re.match(pattern, line) is not None: sectionfound = True
                        if sectionfound == True and line.startswith(rawkey):
                            comments = ""
                            comment_index = 0
                            try:
                                comment_index = line.index('#')
                                comments = line[comment_index:-1]
                            except ValueError:
                                pass
                            line = "{}: {}".format(rawkey, rawvalue )
                            # Left justifying comment with spaces so its in original position
                            line = line.ljust(comment_index - 1, " ")

                            line = "{} {}\n".format(line, comments)
                            sectionfound = False
                            taskdone = True
                        dataout += line
                if taskdone:
                    f=open(file_path, 'w')
                    f.write(dataout)
                    f.close
                    taskdone = False
                    msg +='\n<span class=info--text>Saved {}:{} in {} section to configuration file</span>'.format(rawkey, rawvalue, rawsection)
                    self.logger.info(msg)
                    return
        msg +='\n<span class=info--text>Key {} not found in section {}, cannot update</span>'.format(rawkey, rawsection)
        self.logger.info(msg)

    def TcmdAssign(self, CUR_LANE):
        if CUR_LANE.map == 'NONE' :
            for x in range(99):
                cmd = 'T'+str(x)
                if cmd not in self.AFC.tool_cmds:
                    CUR_LANE.map = cmd
                    break
        self.AFC.tool_cmds[CUR_LANE.map]=CUR_LANE.name
        try:
            self.AFC.gcode.register_command(CUR_LANE.map, self.AFC.cmd_CHANGE_TOOL, desc=self.AFC.cmd_CHANGE_TOOL_help)
        except:
            self.logger.info("Error trying to map lane {lane} to {tool_macro}, please make sure there are no macros already setup for {tool_macro}".format(lane=[CUR_LANE.name], tool_macro=CUR_LANE.map), )
        self.AFC.save_vars()

    def is_homed(self):
        """
        Helper function to determine if printer is currently homed

        :return boolean: True if xyz is homed
        """
        curtime = self.AFC.reactor.monotonic()
        kin_status = self.AFC.toolhead.get_kinematics().get_status(curtime)
        if ('x' not in kin_status['homed_axes'] or 'y' not in kin_status['homed_axes'] or 'z' not in kin_status['homed_axes']):
            return False
        else:
            return True

    def is_moving(self):
        '''
        Helper function to return if the printer is moving or not. This is different from `is_printing` as it will return true if anything in the printer is moving.

        :return boolean: True if anything in the printer is moving
        '''
        eventtime = self.AFC.reactor.monotonic()
        idle_timeout = self.printer.lookup_object("idle_timeout")
        return idle_timeout.get_status(eventtime)["state"] == "Printing"

    def in_print(self):
        """
        Helper function to help determine if printer is in a print by checking print_stats object. Printer is printing if state is not in standby or error

        :return boolean: True if state is not standby or error
        """
        print_stats_idle_states = ['standby', 'error']
        eventtime = self.AFC.reactor.monotonic()
        print_stats = self.printer.lookup_object("print_stats")
        print_state = print_stats.get_status(eventtime)["state"]
        return print_state not in print_stats_idle_states

    def is_printing(self, check_movement=False):
        '''
        Helper function to return if the printer is printing an object.

        :param check_movement: When set to True will also return True if anything in the printer is also moving

        :return boolean: True if printer is printing an object or if printer is moving when `check_movement` is True
        '''
        eventtime = self.AFC.reactor.monotonic()
        print_stats = self.printer.lookup_object("print_stats")
        moving = False

        if check_movement:
            moving = self.is_moving()

        return print_stats.get_status(eventtime)["state"] == "printing" or moving

    def is_paused(self):
        """
        Helper function that returns true if printer is currently paused

        :return boolean: True when printer is paused
        """
        eventtime = self.AFC.reactor.monotonic()
        pause_resume = self.printer.lookup_object("pause_resume")
        return bool(pause_resume.get_status(eventtime)["is_paused"])

    def get_current_lane(self):
        """
        Helper function to lookup current lane name loaded into active toolhead

        :return string: Current lane name that is loaded, None if nothing is loaded
        """
        if self.printer.state_message == 'Printer is ready':
            current_extruder = self.AFC.toolhead.get_extruder().name
            if current_extruder in self.AFC.tools:
                return self.AFC.tools[current_extruder].lane_loaded
        return None

    def get_current_lane_obj(self):
        """
        Helper function to lookup and return current lane object that is loaded into the active toolhead

        :return object: None if nothing is loaded, AFC_stepper object if a lane is currently loaded
        """
        curr_lane_obj = None
        curr_lane = self.get_current_lane()
        if curr_lane in self.AFC.lanes:
            curr_lane_obj = self.AFC.lanes[curr_lane]
        return curr_lane_obj

    def verify_led_object(self, led_name):
        """
        Helper function to lookup AFC_led object.

        :params led_name: name of AFC_led object to lookup

        :return (string, object): error_string if AFC_led object is not found, led object if found
        """
        error_string = ""
        led = None
        afc_object = 'AFC_led '+ led_name.split(':')[0]
        try:
            led = self.printer.lookup_object(afc_object)
        except:
            error_string = "Error: Cannot find [{}] in config, make sure led_index in config is correct for AFC_stepper {}".format(afc_object, led_name.split(':')[-1])
        return error_string, led

    def afc_led (self, status, idx=None):
        if idx == None:
            return

        error_string, led = self.verify_led_object(idx)
        if led is not None:
            led.led_change(int(idx.split(':')[1]), status)
        else:
            self.logger.info( error_string )

    def get_filament_status(self, CUR_LANE):
        if CUR_LANE.prep_state:
            if CUR_LANE.load_state:
                if CUR_LANE.extruder_obj is not None and CUR_LANE.extruder_obj.lane_loaded == CUR_LANE.name:
                    return 'In Tool:' + self.HexConvert(CUR_LANE.led_tool_loaded).split(':')[-1]
                return "Ready:" + self.HexConvert(CUR_LANE.led_ready).split(':')[-1]
            return 'Prep:' + self.HexConvert(CUR_LANE.led_prep_loaded).split(':')[-1]
        return 'Not Ready:' + self.HexConvert(CUR_LANE.led_not_ready).split(':')[-1]

    def handle_activate_extruder(self):
        """
        Function used to deactivate lanes motors and buffers, then enables current extruders lane

        This will also be tied to a callback once multiple extruders are implemented
        """
        cur_lane_loaded = self.get_current_lane_obj()

        # Disable extruder steppers for non active lanes
        for key, obj in self.AFC.lanes.items():
            if cur_lane_loaded is None or key != cur_lane_loaded.name:
                obj.do_enable(False)
                obj.disable_buffer()
                self.afc_led(obj.led_ready, obj.led_index)

        # Exit early if lane is None
        if cur_lane_loaded is None:
            self.AFC.SPOOL.set_active_spool('')
            return

        # Switch spoolman ID
        self.AFC.SPOOL.set_active_spool(cur_lane_loaded.spool_id)
        # Set lanes tool loaded led
        self.afc_led(cur_lane_loaded.led_tool_loaded, cur_lane_loaded.led_index)
        # Enable stepper
        cur_lane_loaded.do_enable(True)
        # Enable buffer
        cur_lane_loaded.enable_buffer()

    def unset_lane_loaded(self):
        """
        Helper function to get current lane and unsync lane from toolhead extruder
        """
        cur_lane_loaded = self.get_current_lane_obj()
        if cur_lane_loaded is not None:
            cur_lane_loaded.unsync_to_extruder()
            cur_lane_loaded.set_unloaded()
            self.AFC.FUNCTION.handle_activate_extruder()
            self.logger.info("Manually removing {} loaded from toolhead".format(cur_lane_loaded.name))
            self.AFC.save_vars()

    def log_toolhead_pos(self):
        msg = "Position: {}".format(self.AFC.toolhead.get_position())
        msg += " base_position: {}".format(self.AFC.gcode_move.base_position)
        msg += " last_position: {}".format(self.AFC.gcode_move.last_position)
        msg += " homing_position: {}".format(self.AFC.gcode_move.homing_position)
        msg += " speed: {}".format(self.AFC.gcode_move.speed)
        msg += " absolute_coord: {}\n".format(self.AFC.gcode_move.absolute_coord)
        self.logger.debug(msg, only_debug=True)

    def HexConvert(self,tmp):
        led=tmp.split(',')
        if float(led[0])>0:
            led[0]=int(255*float(led[0]))
        else:
            led[0]=0
        if float(led[1])>0:
            led[1]=int(255*float(led[1]))
        else:
            led[1]=0
        if float(led[2])>0:
            led[2]=int(255*float(led[2]))
        else:
            led[2]=0

        return '#{:02x}{:02x}{:02x}'.format(*led)

    cmd_AFC_CALIBRATION_help = 'open prompt to begin calibration by selecting Unit to calibrate'
    def cmd_AFC_CALIBRATION(self, gcmd):
        """
        Open a prompt to start AFC calibration by selecting a unit to calibrate. Creates buttons for each unit and
        allows the option to calibrate all lanes across all units.

        Usage:`AFC_CALIBRATION`
        Example: `AFC_CALIBRATION`
        Args:
            None

        Returns:
            None
        """
        prompt = AFCprompt(gcmd, self.logger)
        buttons = []
        title = 'AFC Calibration'
        text = ('The following prompts will lead you through the calibration of your AFC unit(s).'
                ' First, select a unit to calibrate.'
                ' *All values will be automatically updated in the appropriate config sections.')
        for index, (key, item) in enumerate(self.AFC.units.items()):
            # Create a button for each unit
            button_label = "{}".format(key)
            button_command = 'UNIT_CALIBRATION UNIT={}'.format(key)
            button_style = "primary" if index % 2 == 0 else "secondary"
            buttons.append((button_label, button_command, button_style))

        bow_footer = [("All Lanes in all units", "ALL_CALIBRATION", "secondary")]
        prompt.create_custom_p(title, text, buttons,
                               True, None, bow_footer)

    cmd_ALL_CALIBRATION_help = 'open prompt to begin calibration to confirm calibrating all lanes'
    def cmd_ALL_CALIBRATION(self, gcmd):
        """
        Open a prompt to confirm calibration of all lanes in all units. Provides 'Yes' to confirm and 'Back' to
        return to the previous menu.

        Usage:`ALL_CALIBRATION`
        Example: `ALL_CALIBRATION`
        Args:
            None

        Returns:
            None
        """
        prompt = AFCprompt(gcmd, self.logger)
        footer = []
        title = 'Calibrate all'
        text = ('Press Yes to confirm calibrating all lanes in all units')
        footer.append(('Back', 'AFC_CALIBRATION', 'info'))
        footer.append(("Yes", "CALIBRATE_AFC LANE=all", "error"))

        prompt.create_custom_p(title, text, None,
                               True, None, footer)


    cmd_CALIBRATE_AFC_help = 'calibrate the dist hub for lane and then afc_bowden_length'
    def cmd_CALIBRATE_AFC(self, gcmd):
        """
        This function performs the calibration of the hub and Bowden length for one or more lanes within an AFC
        (Automated Filament Control) system. The function uses precise movements to adjust the positions of the
        steppers, check the state of the hubs and tools, and calculate distances for calibration based on the
        user-provided input. If no specific lane is provided, the function defaults to notifying the user that no lane has been selected. The function also includes
        the option to calibrate the Bowden length for a particular lane, if specified.

        Usage:`CALIBRATE_AFC LANE=<lane> DISTANCE=<distance> TOLERANCE=<tolerance> BOWDEN=<lane>`
        Examples:
            - `CALIBRATE_AFC LANE=all Bowden=lane1 DISTANCE=30 TOLERANCE=3`
            - `CALIBRATE_AFC BOWDEN=lane1` (Calibrates the Bowden length for 'lane1')

        Args:
            gcmd: The G-code command object containing the parameters for the command.
                Parameters:
                - LANES: Specifies the lane to calibrate. If not provided, calibrates no lanes.
                - DISTANCE: The distance to move during calibration (optional, defaults to 25mm).
                - TOLERANCE: The tolerance for fine adjustments during calibration (optional, defaults to 5mm).
                - BOWDEN: Specifies the lane to perform Bowden length calibration (optional).
                - UNIT: Specifies the unit to be used in calibration (optional)

        Returns:
            None
        """
        dis    = gcmd.get_float('DISTANCE' , 25)
        tol    = gcmd.get_float('TOLERANCE', 5)
        afc_bl = gcmd.get(      'BOWDEN'   , None)
        lanes  = gcmd.get(      'LANE'     , None)
        unit   = gcmd.get(      'UNIT'     , None)

        if self.AFC.current is not None and afc_bl is not None:
            self.logger.info('Tool must be unloaded to calibrate Bowden length')
            return

        cal_msg = ''
        # Check to make sure lane and unit is valid
        if lanes is not None and lanes != 'all' and lanes not in self.AFC.lanes:
            self.AFC.ERROR.AFC_error("'{}' is not a valid lane".format(lanes), pause=False)
            return

        if unit is not None and unit not in self.AFC.units:
            self.AFC.ERROR.AFC_error("'{}' is not a valid unit".format(unit), pause=False)
            return

        if afc_bl is not None and afc_bl not in self.AFC.lanes:
            self.AFC.ERROR.AFC_error("'{}' is not a valid lane to calibrate bowden length".format(afc_bl), pause=False)
            return

        # Determine if a specific lane is provided
        if lanes is not None:
            self.logger.info('Starting AFC distance Calibrations')
            cal_msg += 'AFC Calibration distances +/-{}mm'.format(tol)
            if unit is None:
                if lanes != 'all':
                    CUR_LANE=self.AFC.lanes[lanes]
                    checked, msg = CUR_LANE.unit_obj.calibrate_lane(CUR_LANE, tol)
                    if(not checked): return
                    cal_msg += msg
                else:
                    # Calibrate all lanes if no specific lane is provided
                    for CUR_LANE in self.AFC.lanes.values():
                        # Calibrate the specific lane
                        checked, msg = CUR_LANE.unit_obj.calibrate_lane(CUR_LANE, tol)
                        if(not checked): return
                        cal_msg += msg
            else:
                if lanes != 'all':
                    CUR_LANE=self.AFC.lanes[lanes]
                    checked, msg = CUR_LANE.unit_obj.calibrate_lane(CUR_LANE, tol)
                    if(not checked): return
                    cal_msg += msg
                else:
                    CUR_UNIT = self.AFC.units[unit]
                    self.logger.info('{}'.format(CUR_UNIT.name))
                    # Calibrate all lanes if no specific lane is provided
                    for CUR_LANE in CUR_UNIT.lanes.values():
                        # Calibrate the specific lane
                        checked, msg = CUR_UNIT.calibrate_lane(CUR_LANE, tol)
                        if(not checked): return
                        cal_msg += msg

            self.logger.info("Lane calibration Done!")

        else:
            self.logger.info('No lanes selected to calibrate dist_hub')

        # Calibrate Bowden length with specified lane
        if afc_bl is not None:
            set_tool_start_back_to_none = False
            CUR_LANE=self.AFC.lanes[afc_bl]

            # Setting tool start to buffer if only tool_end is set and user has buffer so calibration can run
            if CUR_LANE.extruder_obj.tool_start is None and CUR_LANE.extruder_obj.tool_end is not None and CUR_LANE.buffer_obj is not None:
                self.logger.info("Cannot run calibration using post extruder sensor, using buffer to calibrate bowden length")
                CUR_LANE.extruder_obj.tool_start = "buffer"
                set_tool_start_back_to_none = True
            else:
                # Cannot calibrate
                self.AFC.ERROR.AFC_error("Cannot calibrate with only post extruder sensor and no turtleneck buffer defined in config", pause=False)
                return

            self.logger.info('Starting AFC distance Calibrations')
            cal_msg += 'AFC Calibration distances +/-{}mm'.format(tol)
            cal_msg += '\n<span class=info--text>Update values in AFC_Hardware.cfg</span>'
            CUR_LANE.unit_obj.calibrate_bowden(CUR_LANE, dis, tol)
            self.logger.info("Bowden length calibration Done!")

            if set_tool_start_back_to_none:
                CUR_LANE.extruder_obj.tool_start = None

    cmd_SET_BOWDEN_LENGTH_help = "Helper to dynamically set length of bowden between hub and toolhead. Pass in HUB if using multiple box turtles"
    def cmd_SET_BOWDEN_LENGTH(self, gcmd):
        """
        This function adjusts the length of the Bowden tube between the hub and the toolhead.
        It retrieves the hub specified by the 'HUB' parameter and the length adjustment specified
        by the 'LENGTH' parameter. If the hub is not specified and a lane is currently loaded,
        it uses the hub of the current lane.

        Usage: `SET_BOWDEN_LENGTH HUB=<hub> LENGTH=<length>`
        Example: `SET_BOWDEN_LENGTH HUB=Turtle_1 LENGTH=100`

        Args:
            gcmd: The G-code command object containing the parameters for the command.
                  Expected parameters:
                  - HUB: The name of the hub to be adjusted (optional).
                  - LENGTH: The length adjustment value (optional).

        Returns:
            None
        """
        hub           = gcmd.get("HUB", None )
        length_param  = gcmd.get('LENGTH', None)

        # If hub is not passed in try and get hub if a lane is currently loaded
        if hub is None and self.AFC.current is not None:
            CUR_LANE = self.AFC.lanes[self.current]
            hub     = CUR_LANE.hub_obj.name
        elif hub is None and self.current is None:
            self.logger.info("A lane is not loaded please specify hub to adjust bowden length")
            return

        CUR_HUB = self.AFC.hubs[hub]
        config_bowden = CUR_HUB.afc_bowden_length

        if length_param is None or length_param.strip() == '':
            bowden_length = CUR_HUB.config_bowden_length
        else:
            if length_param[0] in ('+', '-'):
                bowden_value = float(length_param)
                bowden_length = config_bowden + bowden_value
            else:
                bowden_length = float(length_param)

        CUR_HUB.afc_bowden_length = bowden_length
        msg =  '// Hub : {}\n'.format( hub )
        msg += '//   Config Bowden Length:   {}\n'.format(CUR_HUB.config_bowden_length)
        msg += '//   Previous Bowden Length: {}\n'.format(config_bowden)
        msg += '//   New Bowden Length:      {}\n'.format(bowden_length)
        msg += '\n// TO SAVE BOWDEN LENGTH afc_bowden_length MUST BE UPDATED IN AFC_Hardware.cfg for each hub if there are multiple'
        self.logger.raw(msg)

    cmd_HUB_CUT_TEST_help = "Test the cutting sequence of the hub cutter, expects LANE=laneN"
    def cmd_HUB_CUT_TEST(self, gcmd):
        """
        This function tests the cutting sequence of the hub cutter for a specified lane.
        It retrieves the lane specified by the 'LANE' parameter, performs the hub cut,
        and responds with the status of the operation.

        Usage: `HUB_CUT_TEST LANE=<lane>`
        Example: `HUB_CUT_TEST LANE=lane1`

        Args:
            gcmd: The G-code command object containing the parameters for the command.
                  Expected parameter:
                  - LANE: The name of the lane to be tested.

        Returns:
            None
        """
        lane = gcmd.get('LANE', None)
        self.logger.info('Testing Hub Cut on Lane: ' + lane)
        if lane not in self.AFC.lanes:
            self.logger.info('{} Unknown'.format(lane))
            return
        CUR_LANE = self.AFC.lanes[lane]
        CUR_HUB = CUR_LANE.hub_obj
        CUR_HUB.hub_cut(CUR_LANE)
        self.logger.info('Hub cut Done!')

    cmd_TEST_help = "Test Assist Motors"
    def cmd_TEST(self, gcmd):
        """
        This function tests the assist motors of a specified lane at various speeds.
        It performs the following steps:
        1. Retrieves the lane specified by the 'LANE' parameter.
        2. Tests the assist motor at full speed, 50%, 30%, and 10% speeds.
        3. Reports the status of each test step.

        Usage: `TEST LANE=<lane>`
        Example: `TEST LANE=lane1`

        Args:
            gcmd: The G-code command object containing the parameters for the command.
                  Expected parameter:
                  - LANE: The name of the lane to be tested.

        Returns:
            None
        """
        lane = gcmd.get('LANE', None)
        if lane == None:
            self.AFC.ERROR.AFC_error('Must select LANE', False)
            return
        self.logger.info('TEST ROUTINE')
        if lane not in self.AFC.lanes:
            self.logger.info('{} Unknown'.format(lane))
            return
        CUR_LANE = self.AFC.lanes[lane]
        self.logger.info('Testing at full speed')
        CUR_LANE.assist(-1)
        self.AFC.reactor.pause(self.AFC.reactor.monotonic() + 1)
        if CUR_LANE.afc_motor_rwd.is_pwm:
            self.logger.info('Testing at 50 percent speed')
            CUR_LANE.assist(-.5)
            self.AFC.reactor.pause(self.AFC.reactor.monotonic() + 1)
            self.logger.info('Testing at 30 percent speed')
            CUR_LANE.assist(-.3)
            self.AFC.reactor.pause(self.AFC.reactor.monotonic() + 1)
            self.logger.info('Testing at 10 percent speed')
            CUR_LANE.assist(-.1)
            self.AFC.reactor.pause(self.AFC.reactor.monotonic() + 1)
        self.logger.info('Test routine complete')
        CUR_LANE.assist(0)

class afcDeltaTime:
    def __init__(self, AFC):
        self.logger = AFC.logger
        self.start_time = None
        self.last_time  = None

    def set_start_time(self):
        self.major_delta_time = self.last_time = self.start_time = datetime.now()

    def log_with_time(self, msg, debug=True):
        curr_time = datetime.now()
        delta_time = (curr_time - self.last_time ).total_seconds()
        total_time = (curr_time - self.start_time).total_seconds()
        msg = "{} (Δt:{:.3f}s, t:{:.3f})".format( msg, delta_time, total_time )
        if debug:
            self.logger.debug( msg )
        else:
            self.logger.info( msg )
        self.last_time = curr_time

    def log_major_delta(self, msg, debug=True):
        curr_time = datetime.now()
        delta_time = (curr_time - self.major_delta_time ).total_seconds()
        msg = "{} t:{:.3f}".format( msg, delta_time )
        self.logger.info( msg )
        self.major_delta_time = curr_time

    def log_total_time(self, msg):
        total_time = (datetime.now() - self.start_time).total_seconds()
        msg = "{} t:{:.3f}".format( msg, total_time )

        self.logger.info( msg )