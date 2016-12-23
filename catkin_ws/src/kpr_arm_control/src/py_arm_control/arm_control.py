#!/usr/bin/env python

import sys
import rospy
import moveit_msgs.msg
import moveit_commander
import smach
import smach_ros
import tf
from cucumber_msgs.srv import HarvestAction, HarvestActionResponse, DebugMove, DebugMoveResponse
from geometry_msgs.msg import Pose, Quaternion
from moveit_msgs.msg import AttachedCollisionObject
from ur_msgs.msg import IOStates
from ur_msgs.srv import SetIO, SetIORequest

import states as state
import sceneObjects as sceneObj

success = {
	"OK": HarvestActionResponse.OK,
	"GRAB_ERR": HarvestActionResponse.GRAB_ERR,
	"CUTT_ERR": HarvestActionResponse.CUTT_ERR,
	"DROP_ERR": HarvestActionResponse.DROP_ERR,
	"MOVE_ERR": HarvestActionResponse.MOVE_ERR
}
startingPosition = Pose()
ATTEMPTS = 10

analogPinStates = [False] * 10
digitalPinStates = [False] * 10

def createStateMachine():
	'''
	Create a fresh state machine.

	@return a state machine object
	'''
	global startingPosition
	sm = smach.StateMachine(outcomes=['OK', 'GRAB_ERR', 'CUTT_ERR', 'DROP_ERR', 'MOVE_ERR'])
	sm.userdata.startingPosition = startingPosition

	with sm:											
		smach.StateMachine.add('MoveToCucumber', state.MoveToCucumber(moveArmTo), 
							   transitions={'MoveOK':'CloseGripper',
											'MoveError':'MOVE_ERR'},
							   remapping={	'data':'request'})
											
		smach.StateMachine.add('CloseGripper', state.CloseGripper(writeWithDigitalFeedback), 
							   transitions={'GripperClosed':'VacuumGrip',
											'GripperError':'GRAB_ERR'})
											
		smach.StateMachine.add('VacuumGrip', state.VacuumGrip(writeWithDigitalFeedback), 
							   transitions={'VacuumCreated':'Cut',
											'VacuumError':'GRAB_ERR'})
											
		smach.StateMachine.add('Cut', state.Cut(writeWithDigitalFeedback), 
							   transitions={'StemCutted':'MoveToDropoff',
											'CutterError':'CUTT_ERR'})
											
		smach.StateMachine.add('MoveToDropoff', state.MoveToDropoff(moveArmTo), 
							   transitions={'MoveOK':'OpenGripper',
											'MoveError':'MOVE_ERR'},
							   remapping={	'data':'request'})
											
		smach.StateMachine.add('OpenGripper', state.OpenGripper(writeWithDigitalFeedback), 
							   transitions={'GripperOpened':'MoveToStart',
											'GripperError':'GRAB_ERR'})
											
		smach.StateMachine.add('MoveToStart', state.MoveToStart(moveArmTo), 
							   transitions={'MoveOK':'OK',
											'MoveError':'MOVE_ERR'},
							   remapping={	'start':'startingPosition'})
	return sm

def moveArmTo(pose_target):
	'''
	Plan the movement to the specified position and move the arm.

	@param pose_target: The Pose of the goal posisiton
	@return True when the move succeded, False otherwise
	'''
	global group, robot
	group.set_start_state_to_current_state()
	group.clear_pose_targets()
	group.set_pose_target(pose_target)
	if not group.plan():
		return False
	return group.go(wait=True)

def moveArmToDebug (req):
	'''
	Debug function for moving the arm without having to create a cucumber.
	
	@param req: The DebugMove request sent
	@return A DebugMoveResponse with the success codes
	'''
	position = req.position
	rotation = req.rotationRPY
	pose = Pose()
	pose.position.x = position.x
	pose.position.y = position.y
	pose.position.z = position.z
	pose.orientation = Quaternion(*tf.transformations.quaternion_from_euler(rotation.x, rotation.y, rotation.z))
	success = moveArmTo(pose)
	if not success :
		return DebugMoveResponse(DebugMoveResponse.MOVE_ERR)
	else :
		return DebugMoveResponse(DebugMoveResponse.OK)

def getCucumberCallback (req):
	'''
	Callback for the cucumber targets.
	Starts the state machine with the provided data

	@param req: The HarvestAction request sent
	@return A HarvestActionResponse with the success codes
	'''
	sm = createStateMachine()
	sm.userdata.request = req
	outcome = sm.execute()	
	return HarvestActionResponse(success[outcome])

def addSceneObjects(aco_publisher):
	'''
	Add the table and endeffector to the planning scene.

	@param aco_publisher: The publisher for the scene objects
	'''
	global group
	aco_publisher.publish(sceneObj.table())
	aco_publisher.publish(sceneObj.endEffector(group))
	
def setupMoveIt():
	'''
	Initialize the moveIt! framework

	@return A triplet containing the robot- and group commander and the planning scene
	'''
	rospy.sleep(5)
	moveit_commander.roscpp_initialize(sys.argv)
	group = moveit_commander.MoveGroupCommander("manipulator")
	robot = moveit_commander.RobotCommander()
	scene = moveit_commander.PlanningSceneInterface()
	return (robot, scene, group)

def IOStatesCallback(msg):
	'''
	Callback method for receiving io states from the arm.
	Stores the received states.
	'''
	global analogPinStates, digitalPinStates
	for pin in msg.digital_in_states:
		digitalPinStates[pin.pin] = pin.state
		
	for pin in msg.analog_in_states:
		analogPinStates[pin.pin] = pin.state
	
def setIO(pin, value):
	'''
	Set the Value of a specified IO pin.
	Note that it only writes to digital pins as we currently are not using the analog pins.

	@param pin: The pin to write to
	@param value: The value that is written to the pin
	@return True when the value is set correctly, False otherwise
	'''
	try:
		set_io = rospy.ServiceProxy('set_io', SetIO)
		#set_io = rospy.ServiceProxy('set_io_testing', SetIO)	# Use these lines when testing using robot.launch
		return set_io(SetIORequest.FUN_SET_DIGITAL_OUT, pin, value)
	except rospy.ServiceException, e:
		rospy.logwarn("Service call failed: %s", e)
		return False

def getAnalog(pin):
	'''
	Get the value of the specified analog pin

	@param pin: The pin to get the value of
	@return The analog value of the pin
	'''
	global analogPinStates
	return analogPinStates[pin]

def getDigital(pin):
	'''
	Get the value of the specified digital pin

	@param pin: The pin to get the value of
	@return The digital value of the pin (True/False)
	'''
	global digitalPinStates
	return digitalPinStates[pin]
	
def setupIO():
	'''
	Setup the IO subscriber and the setIO service.
	@return The subscriber to the io_states channel
	'''
	rospy.wait_for_service('set_io')
	io_states_sub = rospy.Subscriber("io_states", IOStates, IOStatesCallback);
	#rospy.wait_for_service('set_io_testing')											# Use these lines when testing using robot.launch
	#io_states_sub = rospy.Subscriber("io_states_testing", IOStates, IOStatesCallback);	# Use these lines when testing using robot.launch
	return io_states_sub

def writeWithDigitalFeedback(outPin, value, inPin, expected):
	'''
	Write a value to the specified pin and check if another pin reads an expected value.
	This last check is performed multiple times to account for slow changing values.

	@param outPin: The pin to write the value to
	@param value: The value to write to the inPin
	@param inPin: The pin to read from
	@param expected: The expected value on the inPin
	@return True when writing succeeds and the expected value is read, False otherwise
	'''
	if not setIO(outPin, value):
		return False
	
	for i in range(ATTEMPTS):
		if getDigital(inPin) is expected:
			return True
		rospy.sleep(.1)
	return False

def writeWithAnalogFeedback(outPin, value, inPin, expected, delta):
	'''
	Write a value to the specified pin and check if another pin reads an expected value.
	This last check is performed multiple times to account for slow changing values.

	@param outPin: The pin to write the value to
	@param value: The value to write to the inPin
	@param inPin: The pin to read from
	@param expected: The expected value on the inPin
	@param delta: The maximum difference between the read and expected value to be considered equal
	@return True when writing succeeds and the expected value is read, False otherwise
	'''
	if not setIO(outPin, value):
		return False
	
	for i in range(ATTEMPTS):
		if abs(getAnalog(inPin) - expected) < delta:
			return True
		rospy.sleep(.1)
	return False

if __name__ == '__main__':
	'''
	Main method of arm_control.py.
	Starts moveIt! and registers subscribers/publishers.
	'''
	rospy.init_node('ArmControl')
	s = rospy.Service('target/cucumber', HarvestAction, getCucumberCallback)
	aco_pub = rospy.Publisher('attached_collision_object', AttachedCollisionObject, queue_size=10)
	s2 = rospy.Service('/debug/target', DebugMove, moveArmToDebug)
	(robot, scene, group) = setupMoveIt()
	io_states_sub = setupIO()
	startingPosition = group.get_current_pose()
	rospy.loginfo("Started")
	addSceneObjects(aco_pub)
	rospy.spin()
	moveit_commander.roscpp_shutdown()
	rospy.loginfo("Stopped")